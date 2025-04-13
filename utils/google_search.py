import asyncio
import threading
import os
import json
from playwright.async_api import async_playwright
import requests
from openai import OpenAI

client = OpenAI()

web_scraper_response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "scraper_response",
        "schema": {
            "type": "object",
            "properties": {
                "success": {
                    "type": "boolean",
                    "description": "Whether the request was successful. A request is successful if the information has been found in the page."
                },
                "data": {
                    "type": "string",
                    "description": "The extracted information from the page to answer the request/task. Please use some formatting and write clear sentences. Leave empty if the information is not found."
                },
            },
            "required": ["success", "data"],
            "additionalProperties": False
        },
        "strict": True
    }
}

google_agent_response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "google_agent_response",
        "schema": {
            "type": "object",
            "properties": {
                "is_sufficient": {
                    "type": "boolean",
                    "description": "Whether the results are sufficient to answer the request/task. Only set to true if all the information is found. Do not set this to true if the information is only partially found. If you are not sure if the information is sufficient, set this to false."
                },
                "extracted_info": {
                    "type": "string",
                    "description": "The extracted information from the search results. Only give the information wanted, you can't give links. Leave empty if the information is not found. Please use some formatting and write clear sentences."
                },
            },
            "required": ["is_sufficient", "extracted_info"],
            "additionalProperties": False
        },
        "strict": True
    }
}

# --- Google Custom Search API code ---
search_url = "https://www.googleapis.com/customsearch/v1?"
params = {
    "key": os.environ.get("GOOGLE_CUSTOM_SEARCH_API_KEY", "AIzaSyCquE-ly7aS8feL2pJxxyga7zlLn9EIocc"),
    "cx": "b45b1aef650cd4101"
}

def google_custom_search(query: str):
    print("searching google")
    params["q"] = query
    response = requests.get(search_url, params=params)
    items = response.json().get("items", [])
    final_response = [
        {"title": item["title"], "link": item["link"], "snippet": item["snippet"]}
        for item in items
    ]
    return final_response

# --- Global variables for Playwright ---
# These will be set on our background event loop.
playwright_instance = None
browser = None

# --- Asynchronous functions that run on the background loop ---
async def init_playwright():
    """Initialize Playwright and the browser."""
    global playwright_instance, browser
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch()

async def shutdown_playwright():
    """Shutdown the browser and Playwright."""
    global playwright_instance, browser
    if browser:
        await browser.close()
        browser = None
    if playwright_instance:
        await playwright_instance.stop()
        playwright_instance = None

async def scrape_page(page_url: str):
    """Open a page, extract its text (from the <body>) and return it."""
    print("scraping page " + page_url)
    page = await browser.new_page()
    await page.goto(page_url)
    # You can adjust the selector as needed.
    page_text = await page.text_content("body")
    # format correctly the text, removing extra spaces and newlines
    page_text = " ".join(page_text.split())
    await page.close()
    return page_text

async def get_info(page_url: str, info: str):
    """Retrieve page text and then ask the chatbot to extract the requested information."""
    print("getting page text")
    page_text = await scrape_page(page_url)
    print("scraping complete!")
    response = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        temperature=0,
        response_format=web_scraper_response_format,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Here is the full text of a web page. You are looking for the following information: {info}. "
                            "You cannot use your personal knowledge. If the information is not in the page, please let me know. "
                            f"Page text: {page_text}"
                        ),
                    },
                ],
            },
        ]
    )
    # Here we expect the chatbot to return a JSON (as a string) matching our schema.
    return response.choices[0].message.content

# --- Background event loop management ---
# We create a separate thread that runs one dedicated asyncio event loop.

_playwright_loop = None  # type: asyncio.AbstractEventLoop
_playwright_thread = None  # type: threading.Thread

def _start_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def init():
    """
    Synchronously initialize Playwright by starting the background loop and
    scheduling the init_playwright() coroutine.
    """
    global _playwright_loop, _playwright_thread
    _playwright_loop = asyncio.new_event_loop()
    _playwright_thread = threading.Thread(target=_start_loop, args=(_playwright_loop,), daemon=True)
    _playwright_thread.start()
    # Schedule the async initialization.
    future = asyncio.run_coroutine_threadsafe(init_playwright(), _playwright_loop)
    future.result()  # Will raise any initialization exception.
    
def shutdown():
    """
    Synchronously shutdown Playwright by scheduling the shutdown_playwright() coroutine,
    then stopping the background loop.
    """
    global _playwright_loop, _playwright_thread
    if _playwright_loop is None:
        return
    future = asyncio.run_coroutine_threadsafe(shutdown_playwright(), _playwright_loop)
    future.result()
    _playwright_loop.call_soon_threadsafe(_playwright_loop.stop)
    _playwright_thread.join()

def run_async(coro):
    """
    Schedule a coroutine to run in the background playwright loop
    and wait for its result.
    """
    future = asyncio.run_coroutine_threadsafe(coro, _playwright_loop)
    return future.result()

# --- Synchronous functions for external use ---
def start_agent(query: str):
    """
    Run a Google Custom Search (synchronously) and then decide if the search results
    are sufficient to answer the query. If not, scrape up to three pages and ask the
    AI to extract the desired information.
    """
    google_results = google_custom_search(query)

    # Ask the chatbot whether the Google results are sufficient.
    google_agent_response = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        temperature=0,
        response_format=google_agent_response_format,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Here are the search results from Google for the query: {query}. "
                            "Please let me know if the information is sufficient to answer the query. "
                            f"Google results: {google_results}"
                        ),
                    },
                ],
            },
        ]
    ).choices[0].message.content
    print("Google agent response:", google_agent_response)

    # Convert to Python object.
    google_agent_response = json.loads(google_agent_response)
    if google_agent_response["is_sufficient"]:
        print("Google search results are sufficient.")
        return json.dumps({"status": "success", "data": google_agent_response["extracted_info"]})
    else:
        print("Google search results are not sufficient.")
        # Try scraping the first three results.
        for result in google_results[:3]:
            # Use our helper to run the asynchronous function(s) synchronously.
            info_str = run_async(get_info(result["link"], query))
            # Expecting the response to be JSON matching our scraper response format.
            info = json.loads(info_str)
            if info.get("success", False):
                return info["data"]
        # If none of the results provided the info, return an error.
        return json.dumps({"status": "error", "message": "Information not found."})

# --- Initialization ---
init()
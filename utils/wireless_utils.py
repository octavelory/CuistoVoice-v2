#!/usr/bin/env python3
import subprocess
import re

def scan_networks():
    """
    Recherche les réseaux WiFi disponibles en utilisant la commande 'iwlist'.
    Nécessite que l'interface wlan0 soit présente et que le script ait les droits suffisants.
    Retourne une liste de dictionnaires contenant au moins le SSID, la qualité du signal 
    et si le réseau est chiffré.
    """
    try:
        # La commande 'iwlist wlan0 scan' nécessite souvent des droits root.
        result = subprocess.check_output("sudo iwlist wlan0 scan", shell=True, universal_newlines=True)
    except subprocess.CalledProcessError as e:
        print("Erreur lors du scan des réseaux WiFi :", e)
        return []

    networks = []
    cell_data = {}

    for line in result.splitlines():
        line = line.strip()
        # Chaque nouveau "Cell" correspond à un réseau détecté.
        if line.startswith("Cell "):
            if cell_data:
                networks.append(cell_data)
            cell_data = {}

        # Récupération du SSID (nom du réseau)
        essid_match = re.search(r'ESSID:"(.*?)"', line)
        if essid_match:
            cell_data["SSID"] = essid_match.group(1)

        # Récupération de la qualité du signal
        quality_match = re.search(r"Quality=([0-9]+)/([0-9]+)", line)
        if quality_match:
            try:
                quality_value = int(quality_match.group(1))
                quality_max = int(quality_match.group(2))
                cell_data["Quality"] = round(quality_value / quality_max * 100, 2)
            except Exception:
                cell_data["Quality"] = "N/A"

        # Récupération de l'information sur le chiffrement
        if "Encryption key:" in line:
            cell_data["Encryption"] = ("off" not in line)

    if cell_data:
        networks.append(cell_data)

    return networks

def get_registered_networks():
    """
    Lit le fichier de configuration de wpa_supplicant et extrait les SSID des réseaux enregistrés.
    Par défaut le fichier est /etc/wpa_supplicant/wpa_supplicant.conf.
    Retourne la liste des SSID.
    """
    networks = []
    conf_file = "/etc/wpa_supplicant/wpa_supplicant.conf"
    try:
        with open(conf_file, "r") as f:
            content = f.read()
    except Exception as e:
        print("Erreur lors de la lecture du fichier de configuration WiFi :", e)
        return networks

    # On recherche toutes les occurences de ssid="..."
    ssid_matches = re.findall(r'ssid=["\'](.*?)["\']', content)
    networks.extend(ssid_matches)
    return networks

def connect_wifi(ssid, password):
    """
    Ajoute la configuration d'un réseau en générant un bloc de configuration
    grâce à la commande 'wpa_passphrase', en l'ajoutant au fichier de configuration,
    puis en forçant wpa_supplicant à recharger sa configuration.
    
    Paramètres :
      ssid     : le nom du réseau WiFi (string)
      password : le mot de passe associé au réseau (string)
    Retourne True en cas de succès, False sinon.
    """
    try:
        # wpa_passphrase génère un bloc de configuration avec le SSID et le mot de passe fourni.
        cmd = f'wpa_passphrase "{ssid}" "{password}"'
        network_config = subprocess.check_output(cmd, shell=True, universal_newlines=True)
    except Exception as e:
        print("Erreur lors de la génération du bloc de configuration WiFi :", e)
        return False

    conf_file = "/etc/wpa_supplicant/wpa_supplicant.conf"
    try:
        # On ajoute ce bloc à la fin du fichier de configuration.
        with open(conf_file, "a") as f:
            f.write("\n" + network_config)
    except Exception as e:
        print("Erreur lors de l'écriture dans le fichier de configuration :", e)
        return False

    try:
        # On recharge la configuration de wpa_supplicant.
        subprocess.check_call("sudo wpa_cli -i wlan0 reconfigure", shell=True)
    except Exception as e:
        print("Erreur lors du rechargement de la configuration WiFi :", e)
        return False

    print(f"Le réseau {ssid} a été ajouté avec succès et la configuration WiFi a été rechargée.")
    return True

def main():
    print("=== Gestionnaire des réseaux WiFi pour Raspberry Pi ===")
    print("1. Scanner les réseaux disponibles")
    print("2. Lister les réseaux WiFi enregistrés")
    print("3. Se connecter à un nouveau réseau WiFi")
    print("4. Quitter")

    while True:
        choix = input("\nVeuillez choisir une option : ")
        if choix == "1":
            networks = scan_networks()
            if networks:
                print("\nRéseaux disponibles :")
                for i, net in enumerate(networks, start=1):
                    ssid = net.get("SSID", "N/A")
                    quality = net.get("Quality", "N/A")
                    encryption = net.get("Encryption", "N/A")
                    print(f"{i}. SSID : {ssid} - Qualité : {quality}% - Chiffré : {encryption}")
            else:
                print("Aucun réseau détecté ou erreur lors du scan.")
        elif choix == "2":
            networks = get_registered_networks()
            if networks:
                print("\nRéseaux enregistrés dans le fichier de configuration :")
                for i, net in enumerate(networks, start=1):
                    print(f"{i}. {net}")
            else:
                print("Aucun réseau enregistré trouvé.")
        elif choix == "3":
            ssid = input("Entrez le SSID du réseau : ")
            password = input("Entrez le mot de passe du réseau : ")
            connect_wifi(ssid, password)
        elif choix == "4":
            print("Au revoir !")
            break
        else:
            print("Option invalide. Veuillez réessayer.")

if __name__ == "__main__":
    main()
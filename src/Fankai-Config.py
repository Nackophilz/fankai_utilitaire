# -*- coding: utf-8 -*-

import logging
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from getpass import getpass
from pathlib import Path

import pyfiglet
import requests
import urllib3
from github import Github, GithubException
from tqdm import tqdm

# --- Configuration & Initialisation ---
try:
    from plexapi.exceptions import TwoFactorRequired
    from plexapi.myplex import MyPlexAccount
    PLEXAPI_AVAILABLE = True
except ImportError:
    PLEXAPI_AVAILABLE = False

# Désactiver les avertissements de certificat SSL (si nécessaire)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class Config:
    """Centralise la configuration, les chemins et les paramètres de la plateforme."""
    def __init__(self):
        self.current_platform = platform.system()
        self.machine_type = platform.machine()
        self.github_repo = "Nackophilz/fankai_utilitaire"
        self.main_app_name = "Fankai-All"
        
        self._configure_paths_and_settings()
        self._define_tools()

    def _configure_paths_and_settings(self):
        """Définit les chemins et paramètres spécifiques au système d'exploitation."""
        if self.current_platform == 'Windows':
            self.app_data_path = Path(os.getenv('APPDATA', ''))
            self.github_folder = "setup"
            self.file_extension = ".exe"
            self.desktop_path = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        elif self.current_platform == 'Linux':
            self.app_data_path = Path(os.path.expanduser('~/.local/share'))
            self.github_folder = f"setup_linux{'_arm' if self.machine_type == 'aarch64' else ''}"
            self.file_extension = ""
            self.desktop_path = Path(os.environ.get("HOME", "")) / "Desktop"
        elif self.current_platform == 'Darwin':
            self.app_data_path = Path.home() / "Library" / "Application Support"
            self.github_folder = f"setup_macos{'_arm' if self.machine_type == 'arm64' else ''}"
            self.file_extension = ""
            self.desktop_path = Path(os.environ.get("HOME", "")) / "Desktop"
        else:
            raise Exception(f"OS non supporté: {self.current_platform}")

        self.fankai_app_path = self.app_data_path / 'fankai'
        self.setup_path = self.fankai_app_path / 'setup'
        self.log_path = self.fankai_app_path / 'logs'
        self.db_path = self.fankai_app_path / 'fankai.db'
        self.main_executable_path = self.setup_path / f"{self.main_app_name}{self.file_extension}"

    def _define_tools(self):
        """Définit la liste des outils à télécharger."""
        self.tools_to_download = [
            "Fankai-All", "Fankai-Config", "Fankai-Metadata",
            "Fankai-Placement", "Fankai-Service", "Fankai-Sync"
        ]

    def ensure_dirs_exist(self):
        """Crée les répertoires nécessaires."""
        self.setup_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)

def setup_logging(log_path):
    """Configure le logging pour la console et un fichier."""
    logfile = log_path / 'fankai_config.log'
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)
    
    file_handler = logging.FileHandler(logfile, 'w', 'utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

    sys.excepthook = lambda exc_type, exc_value, exc_traceback: \
        logging.critical("Exception non interceptée", exc_info=(exc_type, exc_value, exc_traceback))

# --- Classes Métier ---

class DatabaseManager:
    """Gère toutes les opérations sur la base de données SQLite."""
    def __init__(self, db_path):
        self.db_path = db_path

    def setup_database(self):
        """Crée les tables et insère la configuration par défaut si nécessaire."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
            
            default_config = {
                "plex_ip_publique": "URL_PLEX", "plex_ip_locale": "URL_SECOURS",
                "plex_token": "TOKEN_PLEX", "user_plex": "USER_PLEX",
                "bibliotheque": "NOM_BIBLIOTHEQUE"
            }
            cursor.executemany("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", default_config.items())
            conn.commit()

    def update_config(self, config_data):
        """Met à jour une ou plusieurs clés dans la configuration."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("REPLACE INTO config (key, value) VALUES (?, ?)", config_data.items())
            conn.commit()

class PlexApiManager:
    """Gère l'authentification et l'interaction avec le serveur Plex."""
    def __init__(self, db_manager):
        self.db_manager = db_manager

    def authenticate_and_get_server_details(self):
        """
        Gère le processus complet d'authentification via plex.tv pour récupérer un token et les URLs du serveur.
        """
        if not PLEXAPI_AVAILABLE:
            logging.error("La bibliothèque 'plexapi' est requise pour la configuration initiale.")
            logging.error("Veuillez l'installer avec : pip install plexapi")
            return None

        clear_host()
        logging.info("Configuration de la connexion à votre compte Plex.")
        logging.info("Si l'authentification à deux facteurs (2FA) est activée, préparez votre code.")
        
        max_attempts = 3
        for attempt in range(max_attempts):
            username = input("Adresse e-mail Plex : ")
            password = getpass("Mot de passe Plex : ")
            
            try:
                account = MyPlexAccount(username, password)
            except TwoFactorRequired:
                code = input("Code d'authentification à deux facteurs (2FA) : ")
                account = MyPlexAccount(username, password, code=code)
            except Exception as e:
                logging.error(f"Échec de l'authentification Plex (tentative {attempt + 1}/{max_attempts}): {e}")
                continue
            
            resources = [r for r in account.resources() if r.product == 'Plex Media Server']
            if not resources:
                logging.error("Aucun serveur Plex Media Server trouvé pour ce compte.")
                return None

            if len(resources) > 1:
                print("Plusieurs serveurs trouvés :")
                for i, res in enumerate(resources):
                    print(f"  {i+1}. {res.name} (Propriétaire: {'Oui' if res.owned else 'Non'})")
                choice = int(input("Choisissez le serveur à configurer : ")) - 1
                resource = resources[choice]
            else:
                resource = resources[0]
            
            logging.info(f"Serveur sélectionné : {resource.name}")
            local_conn = next((c.uri for c in resource.connections if c.local), None)
            remote_conn = next((c.uri for c in resource.connections if not c.local), None)
            
            server_details = {
                "token": resource.accessToken,
                "base_url": local_conn or remote_conn,
                "plex_ip_locale": local_conn or "URL_SECOURS",
                "plex_ip_publique": remote_conn or "URL_PLEX",
                "user_plex": username
            }
            
            if not server_details["base_url"]:
                logging.error("Impossible de trouver une adresse de connexion pour ce serveur.")
                return None

            self.db_manager.update_config({
                "plex_token": server_details["token"],
                "plex_ip_locale": server_details["plex_ip_locale"],
                "plex_ip_publique": server_details["plex_ip_publique"],
                "user_plex": server_details["user_plex"]
            })
            logging.info("Authentification et récupération des informations du serveur réussies.")
            return server_details
        
        logging.error("Trop de tentatives d'authentification échouées.")
        return None

    def create_library(self, server_details, library_name, library_path):
        """Crée une nouvelle bibliothèque de séries via un appel API direct."""
        logging.info(f"Tentative de création de la bibliothèque '{library_name}'...")

        url = f"{server_details['base_url']}/library/sections"
        headers = {'X-Plex-Token': server_details['token'], 'Accept': 'application/json'}
        params = {
            'type': 'show',
            'name': library_name,
            'agent': 'com.plexapp.agents.none',
            'scanner': 'Plex Series Scanner',
            'language': 'xn',
            'location': library_path
        }
        
        try:
            response = requests.post(url, headers=headers, params=params, verify=False)
            response.raise_for_status()
            logging.info(f"Bibliothèque '{library_name}' créée avec succès sur Plex !")
            return True
        except requests.RequestException as e:
            logging.error(f"Erreur lors de la création de la bibliothèque Plex: {e}")
            if e.response is not None:
                logging.error(f"Détail de l'erreur: {e.response.text}")
            logging.error("Veuillez vérifier que le chemin est accessible depuis votre serveur Plex.")
            return False

class GitHubUpdater:
    """Gère le téléchargement des outils depuis GitHub."""
    def __init__(self, config):
        self.config = config
        try:
            self.github_api = Github()
            self.repo = self.github_api.get_repo(self.config.github_repo)
        except GithubException as e:
            logging.error(f"Impossible de se connecter à GitHub: {e}")
            self.repo = None

    def download_all_tools(self):
        """Télécharge la dernière version de tous les outils Fankai."""
        if not self.repo:
            logging.error("Téléchargement impossible, dépôt GitHub non accessible.")
            return False
        
        logging.info("Téléchargement des outils Fankai...")
        all_successful = True
        for tool_name in self.config.tools_to_download:
            executable_name = f"{tool_name}{self.config.file_extension}"
            path = f"{self.config.github_folder}/{executable_name}"
            destination = self.config.setup_path / executable_name
            
            try:
                asset = self.repo.get_contents(path)
                response = requests.get(asset.download_url, stream=True, verify=False)
                response.raise_for_status()
                
                total_size = int(response.headers.get('content-length', 0))
                with open(destination, 'wb') as f, tqdm(
                    desc=executable_name, total=total_size, unit='iB',
                    unit_scale=True, unit_divisor=1024
                ) as bar:
                    for data in response.iter_content(chunk_size=1024):
                        size = f.write(data)
                        bar.update(size)

            except Exception as e:
                logging.error(f"Erreur lors du téléchargement de {executable_name}: {e}")
                all_successful = False
        
        if all_successful:
            logging.info("Tous les outils ont été téléchargés avec succès.")
        else:
            logging.error("Certains outils n'ont pas pu être téléchargés.")
        return all_successful

class UIManager:
    """Orchestre l'interaction avec l'utilisateur et le processus de configuration."""
    def __init__(self, config, db_manager, plex_manager, updater):
        self.config = config
        self.db_manager = db_manager
        self.plex_manager = plex_manager
        self.updater = updater

    def run_setup_flow(self):
        """Exécute le processus complet de configuration."""
        server_details = self.plex_manager.authenticate_and_get_server_details()
        if not server_details:
            return
            
        self._configure_plex_library(server_details)

    def _configure_plex_library(self, server_details):
        """Demande les informations pour créer la bibliothèque et la crée."""
        clear_host()
        logging.info("Configuration de la nouvelle bibliothèque FanKai.")
        
        library_name = input("Nom pour la bibliothèque [FanKai] : ") or "FanKai"
        self.db_manager.update_config({"bibliotheque": library_name})
        
        while True:
            logging.info("\nVeuillez indiquer le chemin d'accès au dossier parent de vos Kaï,")
            logging.info("tel qu'il est vu par votre serveur Plex (ex: /data/series/fankai).")
            library_path = input("Chemin d'accès : ")
            
            if self.plex_manager.create_library(server_details, library_name, library_path):
                self._finish_setup()
                break
            else:
                if input("Voulez-vous réessayer ? (o/n) ").lower() != 'o':
                    break

    def _finish_setup(self):
        """Finalise l'installation en téléchargeant les outils et en créant le raccourci."""
        if self.updater.download_all_tools():
            self._create_desktop_shortcut()
            logging.info(f"\nConfiguration terminée ! Vous pouvez maintenant lancer {self.config.main_app_name} depuis votre bureau.")
            
    def _create_desktop_shortcut(self):
        """Crée un raccourci de l'application principale sur le bureau."""
        if not self.config.desktop_path.exists():
            return
        
        shortcut_path = self.config.desktop_path / self.config.main_executable_path.name
        if not shortcut_path.exists():
            try:
                logging.info("Création du raccourci sur le bureau...")
                shutil.copy(self.config.main_executable_path, shortcut_path)
                if self.config.current_platform in ['Linux', 'Darwin']:
                    shortcut_path.chmod(shortcut_path.stat().st_mode | 0o111)
            except Exception as e:
                logging.warning(f"Impossible de créer le raccourci sur le bureau : {e}")

# --- Fonctions utilitaires et Point d'entrée ---

def clear_host():
    os.system('cls' if os.name == 'nt' else 'clear')

def handle_interrupt(sig, frame):
    print("\n\nOpération interrompue. Au revoir !")
    sys.exit(0)

def main():
    """Fonction principale du script."""
    config = Config()
    config.ensure_dirs_exist()
    os.chdir(config.fankai_app_path)
    
    setup_logging(config.log_path)
    
    clear_host()
    print(pyfiglet.figlet_format("FANKAI-CONFIG"))
    time.sleep(1)
    logging.info("Bienvenue dans l'assistant de configuration de Fankai.")
    logging.info("Ce script va vous guider pour connecter votre compte Plex et créer la bibliothèque requise.\n")
    
    db_manager = DatabaseManager(config.db_path)
    db_manager.setup_database()
    
    plex_manager = PlexApiManager(db_manager)
    updater = GitHubUpdater(config)
    ui = UIManager(config, db_manager, plex_manager, updater)
    
    ui.run_setup_flow()

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        main()
    except Exception as e:
        logging.critical(f"Une erreur fatale est survenue: {e}", exc_info=True)
    
    input("\nAppuyez sur Entrée pour quitter.")


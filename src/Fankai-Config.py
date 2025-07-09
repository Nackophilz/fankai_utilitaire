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
from plexapi.exceptions import TwoFactorRequired
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from tqdm import tqdm

# Désactiver les avertissements de certificat SSL (si nécessaire)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuration & Helpers ---

class Config:
    """
    Centralise la configuration, les chemins et la logique dépendante de la plateforme.
    """
    def __init__(self):
        self.current_platform = platform.system()
        self.machine_type = platform.machine()
        self.github_repo = "Nackophilz/fankai_utilitaire"
        self.main_app_name = "Fankai"
        
        self._configure_paths_and_settings()

    def _configure_paths_and_settings(self):
        """Définit les chemins et paramètres spécifiques à l'OS."""
        if self.current_platform == 'Windows':
            self.app_data_path = Path(os.getenv('APPDATA', ''))
            self.github_folder = "setup"
            self.file_extension = ".exe"
            self.desktop_path = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
            self.verify_ssl = True
        elif self.current_platform == 'Linux':
            self.app_data_path = Path(os.path.expanduser('~/.local/share'))
            self.github_folder = f"setup_linux{'_arm' if self.machine_type == 'aarch64' else ''}"
            self.file_extension = ""
            self.desktop_path = Path(os.environ.get("HOME", "")) / "Desktop"
            self.verify_ssl = True
        elif self.current_platform == 'Darwin':
            self.app_data_path = Path.home() / "Library" / "Application Support"
            self.github_folder = f"setup_macos{'_arm' if self.machine_type == 'arm64' else ''}"
            self.file_extension = ""
            self.desktop_path = Path(os.environ.get("HOME", "")) / "Desktop"
            self.verify_ssl = False
        else:
            raise Exception(f"OS non supporté: {self.current_platform}")

        self.fankai_app_path = self.app_data_path / 'fankai'
        self.setup_path = self.fankai_app_path / 'setup'
        self.log_path = self.fankai_app_path / 'logs'
        self.db_path = self.fankai_app_path / 'fankai.db'
        self.main_executable_path = self.setup_path / f"{self.main_app_name}{self.file_extension}"

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

    console_handler = logging.StreamHandler()
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

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def setup_database(self):
        """Crée les tables et insère la configuration par défaut si nécessaire."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
            
            default_config = {
                "plex_ip_publique": "URL_PLEX", "plex_ip_locale": "URL_SECOURS",
                "plex_token": "TOKEN_PLEX", "user_plex": "USER_PLEX", "mdp_plex": "MDP_PLEX",
                "bibliotheque": "NOM_BIBLIOTHEQUE"
            }
            cursor.executemany("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", default_config.items())
            conn.commit()

    def update_config(self, config_data):
        """Met à jour une ou plusieurs clés dans la configuration."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("REPLACE INTO config (key, value) VALUES (?, ?)", config_data.items())
            conn.commit()

    def load_config(self):
        """Charge l'ensemble de la configuration depuis la base de données."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM config")
            return {row[0]: row[1] for row in cursor.fetchall()}

class PlexManager:
    """Gère l'authentification et l'interaction avec le serveur Plex."""
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.plex_server = None

    def authenticate(self, username, password):
        """Authentifie l'utilisateur auprès de MyPlex, gère le 2FA."""
        try:
            return MyPlexAccount(username, password)
        except TwoFactorRequired:
            code = input("Code 2FA Plex: ")
            return MyPlexAccount(username, password, code=code)
        except Exception as e:
            logging.error(f"Échec de l'authentification Plex: {e}")
            return None

    def select_server_and_connections(self, account):
        """Permet à l'utilisateur de choisir un serveur et ses connexions."""
        resources = account.resources()
        if not resources:
            logging.error("Aucun serveur Plex trouvé pour ce compte.")
            return None, None

        if len(resources) > 1:
            print("Plusieurs serveurs Plex trouvés :")
            for i, resource in enumerate(resources, 1):
                print(f"  {i}. {resource.name}")
            choice = int(input("Sélectionnez le serveur à utiliser : ")) - 1
            resource = resources[choice]
        else:
            resource = resources[0]
        
        logging.info(f"Serveur sélectionné : {resource.name}")
        
        local_conn = next((c for c in resource.connections if c.local), None)
        remote_conn = next((c for c in resource.connections if not c.local), None)
        
        return local_conn.uri if local_conn else None, remote_conn.uri if remote_conn else None

    def connect_to_server(self):
        """Tente de se connecter au serveur Plex en utilisant les URL stockées."""
        config = self.db_manager.load_config()
        token = config.get("plex_token")
        urls = [config.get("plex_ip_locale"), config.get("plex_ip_publique")]

        for url in filter(None, urls):
            try:
                self.plex_server = PlexServer(url, token)
                logging.info(f"Connecté avec succès à Plex via {url}")
                return True
            except Exception:
                logging.warning(f"Échec de la connexion à {url}, essai suivant...")
        
        logging.error("Impossible de se connecter au serveur Plex avec les informations fournies.")
        return False

    def create_library(self, library_name, library_path):
        """Crée une nouvelle bibliothèque de séries sur le serveur Plex."""
        if not self.plex_server:
            logging.error("Non connecté à un serveur Plex pour créer la bibliothèque.")
            return False
        
        try:
            self.plex_server.library.add(
                name=library_name, type='show', agent='com.plexapp.agents.none',
                scanner='Plex Series Scanner', location=library_path, language='xn'
            )
            logging.info(f"Bibliothèque '{library_name}' créée avec succès sur Plex !")
            return True
        except Exception as e:
            logging.error(f"Erreur lors de la création de la bibliothèque Plex: {e}")
            logging.error("Veuillez vérifier que le chemin est accessible depuis votre serveur Plex.")
            return False

class GitHubUpdater:
    """Gère le téléchargement de l'application principale depuis GitHub."""
    def __init__(self, config):
        self.config = config
        try:
            self.github_api = Github()
            self.repo = self.github_api.get_repo(self.config.github_repo)
        except GithubException as e:
            logging.error(f"Impossible de se connecter à GitHub: {e}")
            self.repo = None

    def download_main_app(self):
        """Télécharge la dernière version de l'application Fankai."""
        if not self.repo:
            logging.error("Téléchargement impossible, dépôt GitHub non accessible.")
            return False
        
        logging.info("Téléchargement de l'utilitaire Fankai principal...")
        try:
            path = f"{self.config.github_folder}/{self.config.main_app_name}{self.config.file_extension}"
            asset = self.repo.get_contents(path)
            
            response = requests.get(asset.download_url, stream=True, verify=self.config.verify_ssl)
            response.raise_for_status()
            
            with open(self.config.main_executable_path, 'wb') as f:
                f.write(response.content)
            
            logging.info("Téléchargement terminé.")
            return True
        except Exception as e:
            logging.error(f"Erreur lors du téléchargement de l'application: {e}")
            return False

class UIManager:
    """Orchestre l'interaction avec l'utilisateur et le processus de configuration."""
    def __init__(self, config, db_manager, plex_manager, updater):
        self.config = config
        self.db_manager = db_manager
        self.plex_manager = plex_manager
        self.updater = updater

    def run_configuration_flow(self):
        """Exécute le processus complet de configuration."""
        if not self._ensure_plex_credentials():
            return
        
        if not self.plex_manager.connect_to_server():
            if self._ask_for_reconfiguration():
                return self.run_configuration_flow()
            return
            
        self._configure_plex_library()

    def _ensure_plex_credentials(self):
        """Vérifie et demande les identifiants Plex si nécessaire."""
        config = self.db_manager.load_config()
        if config.get('plex_token') == 'TOKEN_PLEX' or config.get('user_plex') == 'USER_PLEX':
            return self._gather_and_save_plex_credentials()
        return True

    def _gather_and_save_plex_credentials(self):
        """Récupère et sauvegarde les informations de connexion Plex."""
        clear_host()
        logging.info("Configuration de la connexion à votre compte Plex.")
        logging.info("Si l'authentification à deux facteurs (2FA) est activée, vous devrez saisir le code.")
        
        max_attempts = 3
        for attempt in range(max_attempts):
            username = input("Adresse e-mail Plex : ")
            password = getpass("Mot de passe Plex : ")
            
            account = self.plex_manager.authenticate(username, password)
            if account:
                local_uri, remote_uri = self.plex_manager.select_server_and_connections(account)
                
                self.db_manager.update_config({
                    "plex_ip_publique": remote_uri or "URL_PLEX",
                    "plex_ip_locale": local_uri or "URL_SECOURS",
                    "plex_token": account.authenticationToken,
                    "user_plex": username,
                    "mdp_plex": password
                })
                logging.info("Authentification et configuration du serveur réussies.")
                return True
            else:
                logging.error(f"Échec de la tentative {attempt + 1}/{max_attempts}. Veuillez réessayer.")

        logging.error("Trop de tentatives d'authentification échouées.")
        return False
        
    def _configure_plex_library(self):
        """Demande les informations pour créer la bibliothèque et la crée."""
        clear_host()
        logging.info("Configuration de la nouvelle bibliothèque FanKai.")
        
        library_name = input("Nom pour la bibliothèque [FanKai] : ") or "FanKai"
        self.db_manager.update_config({"bibliotheque": library_name})
        
        while True:
            logging.info("\nVeuillez indiquer le chemin d'accès au dossier parent de vos Kaï,")
            logging.info("tel qu'il est vu par votre serveur Plex (ex: /data/series/fankai).")
            library_path = input("Chemin d'accès : ")
            
            if self.plex_manager.create_library(library_name, library_path):
                self._finish_setup()
                break
            else:
                if input("Voulez-vous réessayer ? (o/n) ").lower() != 'o':
                    break

    def _ask_for_reconfiguration(self):
        """Demande si l'utilisateur veut reconfigurer la connexion Plex."""
        return input("La connexion a échoué. Voulez-vous reconfigurer les identifiants ? (o/n) ").lower() == 'o'

    def _finish_setup(self):
        """Finalise l'installation en téléchargeant l'app et en créant le raccourci."""
        if self.updater.download_main_app():
            self._create_desktop_shortcut()
            logging.info("\nConfiguration terminée ! Vous pouvez maintenant lancer Fankai depuis votre bureau.")
            
    def _create_desktop_shortcut(self):
        """Crée un raccourci de l'application sur le bureau."""
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
    print("\nSignal d'interruption reçu. Au revoir !")
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
    
    plex_manager = PlexManager(db_manager)
    updater = GitHubUpdater(config)
    ui = UIManager(config, db_manager, plex_manager, updater)
    
    ui.run_configuration_flow()

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        main()
    except Exception as e:
        logging.critical(f"Une erreur fatale est survenue: {e}", exc_info=True)
    
    input("\nAppuyez sur Entrée pour quitter.")

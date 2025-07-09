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
from datetime import datetime
from pathlib import Path

import pyfiglet
import pytz
import requests
import urllib3
from github import Github, GithubException
from termcolor import colored
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
        
        self._configure_paths_and_settings()
        self._define_tools()

    def _configure_paths_and_settings(self):
        """Définit les chemins et paramètres spécifiques à l'OS."""
        if self.current_platform == 'Windows':
            self.app_data_path = Path(os.getenv('APPDATA', ''))
            self.github_folder = "setup"
            self.file_extension = ".exe"
            self.verify_ssl = True
        elif self.current_platform == 'Linux':
            self.app_data_path = Path(os.path.expanduser('~/.local/share'))
            self.github_folder = f"setup_linux{'_arm' if self.machine_type == 'aarch64' else ''}"
            self.file_extension = ""
            self.verify_ssl = True
        elif self.current_platform == 'Darwin':
            self.app_data_path = Path.home() / "Library" / "Application Support"
            self.github_folder = f"setup_macos{'_arm' if self.machine_type == 'arm64' else ''}"
            self.file_extension = ""
            self.verify_ssl = False
        else:
            raise Exception(f"OS non supporté: {self.current_platform}")

        self.fankai_app_path = self.app_data_path / 'fankai'
        self.setup_path = self.fankai_app_path / 'setup'
        self.log_path = self.fankai_app_path / 'logs'
        self.db_path = self.fankai_app_path / 'fankai.db'

    def _define_tools(self):
        """Définit la liste des outils gérés par ce script."""
        self.tools = {
            "service": {"name": "Fankai-Service", "chmod_sync": True},
            "placement": {"name": "Fankai-Placement"},
            "placement_auto": {"name": "Fankai-Placement", "args": ["auto"]},
            "metadata": {"name": "Fankai-Metadata"},
            "sync": {"name": "Fankai-Sync"}
        }

    def get_tool_path(self, tool_key):
        """Retourne le chemin complet d'un outil."""
        tool_name = self.tools.get(tool_key, {}).get("name")
        if not tool_name:
            return None
        return self.setup_path / f"{tool_name}{self.file_extension}"

    def ensure_dirs_exist(self):
        """Crée les répertoires nécessaires."""
        self.setup_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)

def setup_logging(log_path):
    """Configure le logging pour la console et un fichier."""
    logfile = log_path / 'fankai_all.log'
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
            cursor.execute('CREATE TABLE IF NOT EXISTS rename (line TEXT UNIQUE)')
            
            default_config = {
                "fankai_parents": "FANKAI_PARENTS", "fankai_telechargement": "FANKAI_TELECHARGEMENT",
                "plex_plugin": "PLEX_PLUGIN", "service_placement": "SERVICE_PLACEMENT",
                "user_discord": "USER_DISCORD", "issue_numbers": "", "onepiece_choice": ""
            }
            cursor.executemany("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", default_config.items())
            conn.commit()

class GitHubUpdater:
    """Gère la vérification et le téléchargement des mises à jour depuis GitHub."""
    def __init__(self, config):
        self.config = config
        self.timezone = pytz.timezone("Europe/Paris")
        try:
            self.github_api = Github()
            self.repo = self.github_api.get_repo(self.config.github_repo)
        except GithubException as e:
            logging.error(f"Impossible de se connecter à GitHub: {e}")
            self.repo = None

    def check_and_update_all(self):
        """Vérifie et met à jour tous les outils définis dans la configuration."""
        if not self.repo:
            logging.error("Mise à jour impossible: pas de connexion au dépôt GitHub.")
            return

        logging.info("Recherche de mises à jour pour les outils Fankai...")
        unique_tool_names = {tool['name'] for tool in self.config.tools.values()}

        for tool_name in unique_tool_names:
            self._update_tool(tool_name)
        logging.info("Vérification des mises à jour terminée.")

    def _update_tool(self, tool_name):
        """Met à jour un outil spécifique."""
        executable_name = f"{tool_name}{self.config.file_extension}"
        asset_info = self._get_asset_info(executable_name)
        if not asset_info:
            return

        local_path = self.config.setup_path / executable_name
        if self._is_update_needed(local_path, asset_info['last_modified']):
            logging.info(f"'{tool_name}' a été mis à jour, téléchargement...")
            if asset_info['changelog']:
                logging.info(f"  Changelog: {asset_info['changelog']}")
            self._download_asset(asset_info, local_path)
        else:
            logging.info(f"'{tool_name}' est déjà à jour.")
    
    def _get_asset_info(self, executable_name):
        """Récupère les informations sur un fichier du dépôt."""
        try:
            path = f"{self.config.github_folder}/{executable_name}"
            content_file = self.repo.get_contents(path)
            latest_commit = self.repo.get_commits(path=content_file.path)[0]
            
            commit_date_utc = datetime.strptime(latest_commit.last_modified, '%a, %d %b %Y %H:%M:%S %Z')
            return {
                "name": content_file.name, "download_url": content_file.download_url,
                "last_modified": commit_date_utc.replace(tzinfo=pytz.utc).astimezone(self.timezone),
                "changelog": latest_commit.commit.message
            }
        except GithubException:
            logging.warning(f"Impossible de trouver '{executable_name}' dans le dépôt.")
            return None

    def _is_update_needed(self, local_path, remote_mod_time):
        """Vérifie si une mise à jour est nécessaire pour un fichier."""
        if not local_path.exists():
            return True
        
        local_mod_time_ts = local_path.stat().st_mtime
        local_mod_time = datetime.fromtimestamp(local_mod_time_ts, tz=self.timezone)
        return remote_mod_time > local_mod_time

    def _download_asset(self, asset_info, destination):
        """Télécharge un fichier avec une barre de progression."""
        try:
            response = requests.get(asset_info['download_url'], stream=True, verify=self.config.verify_ssl)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            
            with open(destination, 'wb') as f, tqdm(
                desc=asset_info['name'], total=total_size, unit='iB', unit_scale=True, unit_divisor=1024,
            ) as bar:
                for data in response.iter_content(chunk_size=1024):
                    size = f.write(data)
                    bar.update(size)
        except requests.RequestException as e:
            logging.error(f"Erreur de téléchargement pour {asset_info['name']}: {e}")

class AppLauncher:
    """Gère le lancement des exécutables."""
    def __init__(self, config):
        self.config = config

    def launch(self, tool_key):
        """Lance un outil spécifique."""
        tool_info = self.config.tools.get(tool_key)
        if not tool_info:
            logging.error(f"Clé d'outil invalide: {tool_key}")
            return
            
        tool_path = self.config.get_tool_path(tool_key)
        if not tool_path or not tool_path.exists():
            logging.error(f"L'exécutable pour '{tool_info['name']}' est introuvable.")
            return

        logging.info(f"Lancement de {tool_info['name']}...")
        time.sleep(1)
        clear_host()

        if self.config.current_platform in ['Linux', 'Darwin']:
            tool_path.chmod(tool_path.stat().st_mode | 0o111)
            if tool_info.get("chmod_sync"):
                sync_path = self.config.get_tool_path("sync")
                if sync_path and sync_path.exists():
                    sync_path.chmod(sync_path.stat().st_mode | 0o111)

        command = [str(tool_path)] + tool_info.get("args", [])
        
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"L'application s'est terminée avec une erreur: {e}")
        except Exception as e:
            logging.error(f"Impossible de lancer l'application: {e}")

class UIManager:
    """Gère l'interface utilisateur en ligne de commande."""
    def __init__(self, config, launcher):
        self.config = config
        self.launcher = launcher
        self.menu_options = {
            "1": {"key": "service", "desc": "Mettre à jour les packs automatiquement", "note": "EMBY SEULEMENT"},
            "2": {"key": "placement", "desc": "Placer et renommer vos films Kaï téléchargés"},
            "2a": {"key": "placement_auto", "desc": "Placement automatique puis scan des métadonnées", "note": "PLEX SEULEMENT"},
            "3": {"key": "metadata", "desc": "Scanner les métadonnées de votre bibliothèque", "note": "PLEX SEULEMENT"},
        }

    def display_main_menu(self):
        """Affiche le menu principal et gère la sélection de l'utilisateur."""
        while True:
            clear_host()
            print("Quel utilitaire voulez-vous utiliser aujourd'hui ?\n")
            for key, val in self.menu_options.items():
                tool_name = self.config.tools[val['key']]['name']
                note = colored(f"({val['note']})", "red") if 'note' in val else ""
                print(f"{colored(f'({key})', 'green')} {colored(tool_name, 'blue')} -> {val['desc']} {note}")
            
            print(colored("\nPour quitter: CTRL+C ou la croix du terminal.", "red"))

            try:
                choice = input("\nVotre choix : ")
                if choice in self.menu_options:
                    self.launcher.launch(self.menu_options[choice]['key'])
                else:
                    print("Choix invalide. Veuillez réessayer.")
                    time.sleep(2)
            except (KeyboardInterrupt, EOFError):
                handle_interrupt(None, None)

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

    db_manager = DatabaseManager(config.db_path)
    db_manager.setup_database()
    
    clear_host()
    print(pyfiglet.figlet_format("FANKAI-ALL"))
    time.sleep(2)
    
    updater = GitHubUpdater(config)
    updater.check_and_update_all()

    launcher = AppLauncher(config)
    ui = UIManager(config, launcher)
    ui.display_main_menu()

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        main()
    except Exception as e:
        logging.critical(f"Une erreur fatale et non interceptée est survenue: {e}", exc_info=True)
        input("\nAppuyez sur Entrée pour quitter.")

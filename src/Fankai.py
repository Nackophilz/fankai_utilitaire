# -*- coding: utf-8 -*-


import logging
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tkinter import Label, Tk

import pyfiglet
import pytz
import requests
import urllib3
from github import Github, GithubException
from PIL import Image, ImageTk
from tqdm import tqdm

# Désactiver les avertissements de certificat SSL (si nécessaire)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuration & Helpers ---

class Config:
    """
    Centralise la configuration et la logique dépendante de la plateforme.
    """
    def __init__(self):
        self.current_platform = platform.system()
        self.machine_type = platform.machine()
        self.app_name = "Fankai"
        self.executable_name = "Fankai-All"
        self.github_repo = "Nackophilz/fankai_utilitaire"

        self._configure_paths_and_settings()

    def _configure_paths_and_settings(self):
        """Définit les chemins et paramètres spécifiques à l'OS."""
        if self.current_platform == 'Windows':
            self.app_data_path = Path(os.getenv('APPDATA', ''))
            self.github_folder = "setup"
            self.file_extension = ".exe"
            self.verify_ssl = True
        elif self.current_platform == 'Linux':
            self.app_data_path = Path(os.path.expanduser('~/.local/share'))
            self.github_folder = "setup_linux_arm" if self.machine_type == 'aarch64' else "setup_linux"
            self.file_extension = ""
            self.verify_ssl = True
        elif self.current_platform == 'Darwin':
            self.app_data_path = Path.home() / "Library" / "Application Support"
            self.github_folder = "setup_macos_arm" if self.machine_type == 'arm64' else "setup_macos"
            self.file_extension = ""
            self.verify_ssl = False
        else:
            raise Exception(f"OS non supporté: {self.current_platform}")

        self.fankai_app_path = self.app_data_path / self.app_name.lower()
        self.setup_path = self.fankai_app_path / 'setup'
        self.log_path = self.fankai_app_path / 'logs'
        
        self.full_executable_name = f"{self.executable_name}{self.file_extension}"
        self.executable_path = self.setup_path / self.full_executable_name

    def ensure_dirs_exist(self):
        """Crée les répertoires nécessaires s'ils n'existent pas."""
        self.setup_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)

def setup_logging(log_path):
    """Configure le logging pour la console et un fichier."""
    logfile = log_path / 'fankai_launcher.log'
    
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    
    logger.setLevel(logging.INFO)

    # Handler Console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)
    
    # Handler Fichier
    file_handler = logging.FileHandler(logfile, 'a', 'utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

    # Gestion des exceptions non interceptées
    sys.excepthook = lambda exc_type, exc_value, exc_traceback: \
        logging.critical("Exception non interceptée", exc_info=(exc_type, exc_value, exc_traceback))

# --- Classes Métier ---

class GitHubUpdater:
    """
    Gère la vérification et le téléchargement des mises à jour depuis GitHub.
    """
    def __init__(self, config):
        self.config = config
        try:
            self.github_api = Github()
            self.repo = self.github_api.get_repo(self.config.github_repo)
        except GithubException as e:
            logging.error(f"Impossible de se connecter à GitHub. Vérifiez votre connexion ou votre token. Erreur: {e}")
            self.repo = None

    def get_latest_asset_info(self):
        """Récupère les informations sur le dernier exécutable disponible."""
        if not self.repo:
            return None
        try:
            content_file = self.repo.get_contents(
                f"{self.config.github_folder}/{self.config.full_executable_name}"
            )
            latest_commit = self.repo.get_commits(path=content_file.path)[0]
            
            commit_date_utc = datetime.strptime(latest_commit.last_modified, '%a, %d %b %Y %H:%M:%S %Z')
            
            return {
                "name": content_file.name,
                "download_url": content_file.download_url,
                "last_modified": commit_date_utc.replace(tzinfo=pytz.utc),
                "changelog": latest_commit.commit.message
            }
        except GithubException:
            logging.error(f"Impossible de trouver le fichier '{self.config.full_executable_name}' dans le dépôt.")
            return None

    def download_asset(self, asset_info):
        """Télécharge un fichier depuis une URL et affiche une barre de progression."""
        url = asset_info['download_url']
        destination = self.config.executable_path
        
        logging.info(f"Téléchargement de {asset_info['name']}...")
        try:
            response = requests.get(url, stream=True, verify=self.config.verify_ssl)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            
            with open(destination, 'wb') as f, tqdm(
                desc=asset_info['name'],
                total=total_size,
                unit='iB',
                unit_scale=True,
                unit_divisor=1024,
            ) as bar:
                for data in response.iter_content(chunk_size=1024):
                    size = f.write(data)
                    bar.update(size)
            return True
        except requests.RequestException as e:
            logging.error(f"Erreur de téléchargement: {e}")
            return False

class AppManager:
    """
    Gère l'exécutable local.
    """
    def __init__(self, config):
        self.config = config

    def is_update_needed(self, remote_mod_time):
        """Vérifie si une mise à jour est nécessaire."""
        if not self.config.executable_path.exists():
            return True
            
        local_mod_time_ts = self.config.executable_path.stat().st_mtime
        local_mod_time = datetime.fromtimestamp(local_mod_time_ts, tz=pytz.utc)
        
        return remote_mod_time > local_mod_time

    def launch(self):
        """Lance l'application Fankai-All."""
        if not self.config.executable_path.exists():
            logging.error(f"L'exécutable {self.config.full_executable_name} est introuvable.")
            return

        logging.info(f"Lancement de {self.config.app_name}...")
        try:
            if self.config.current_platform in ['Linux', 'Darwin']:
                self.config.executable_path.chmod(self.config.executable_path.stat().st_mode | 0o111)
            subprocess.run([str(self.config.executable_path)], check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"L'application s'est terminée avec une erreur: {e}")
        except Exception as e:
            logging.error(f"Impossible de lancer l'application: {e}")

# --- Interface Utilisateur ---

def show_splash_screen(duration_ms=3000):
    """Affiche un écran de démarrage avec une image depuis une URL."""
    image_url = 'https://gitlab.com/ElPouki/fankai_pack/-/raw/main/assets/fankai.png'
    try:
        root = Tk()
        root.overrideredirect(True)

        response = requests.get(image_url, verify=False)
        response.raise_for_status()
        
        image = Image.open(BytesIO(response.content))
        
        # Redimensionnement de l'image si elle est trop grande
        max_width, max_height = 850, 500
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        
        photo = ImageTk.PhotoImage(image)

        # Centrer la fenêtre
        ws, hs = root.winfo_screenwidth(), root.winfo_screenheight()
        x = (ws / 2) - (image.width / 2)
        y = (hs / 2) - (image.height / 2)
        root.geometry(f'{image.width}x{image.height}+{int(x)}+{int(y)}')

        Label(root, image=photo).pack()
        root.after(duration_ms, root.destroy)
        root.mainloop()
    except Exception as e:
        logging.warning(f"Impossible d'afficher le splash screen: {e}")

def print_banner():
    """Affiche la bannière ASCII."""
    clear_host()
    banner = pyfiglet.figlet_format(Config().app_name.upper())
    print(banner)
    time.sleep(1)

def clear_host():
    """Nettoie la console."""
    os.system('cls' if os.name == 'nt' else 'clear')

def handle_interrupt(sig, frame):
    """Gère le signal d'interruption (Ctrl+C)."""
    logging.info("\nSignal d'interruption reçu. Fermeture du lanceur.")
    sys.exit(0)
    
# --- Point d'entrée ---

def main():
    """
    Fonction principale du lanceur.
    """
    show_splash_screen()
    
    config = Config()
    config.ensure_dirs_exist()
    setup_logging(config.log_path)

    print_banner()

    updater = GitHubUpdater(config)
    app_manager = AppManager(config)

    logging.info("Vérification des mises à jour...")
    asset_info = updater.get_latest_asset_info()

    if not asset_info:
        logging.error("Impossible de vérifier les mises à jour. Tentative de lancement de la version locale.")
        app_manager.launch()
        return

    if app_manager.is_update_needed(asset_info['last_modified']):
        logging.info("Une nouvelle version est disponible.")
        logging.info(f"Changelog: {asset_info['changelog']}")
        if updater.download_asset(asset_info):
            logging.info("Mise à jour terminée avec succès.")
        else:
            logging.error("Échec de la mise à jour. Le lancement est annulé.")
            return
    else:
        logging.info("Vous disposez déjà de la dernière version.")

    app_manager.launch()


if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        main()
    except Exception as e:
        logging.critical(f"Une erreur fatale est survenue: {e}", exc_info=True)
        input("\nAppuyez sur Entrée pour quitter.")

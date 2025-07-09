# -*- coding: utf-8 -*-


import base64
import logging
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import gitlab
import pyfiglet
import shutil
import requests

# --- Configuration & Helpers ---

class Config:
    """
    Centralise la configuration, les chemins et la logique dépendante de la plateforme.
    """
    def __init__(self):
        self.gitlab_project_id = 'ElPouki/fankai_synchro'
        self.current_platform = platform.system()
        self._configure_paths()

    def _configure_paths(self):
        """Définit les chemins spécifiques à l'OS."""
        if self.current_platform == 'Windows':
            app_data_root = Path(os.getenv('APPDATA', ''))
        elif self.current_platform in ['Linux', 'Darwin']:
            app_data_root = Path.home() / ".local" / "share" if self.current_platform == 'Linux' else Path.home() / "Library" / "Application Support"
        else:
            raise Exception(f"OS non supporté: {self.current_platform}")

        self.fankai_app_path = app_data_root / 'fankai'
        self.log_path = self.fankai_app_path / 'logs'
        self.db_path = self.fankai_app_path / 'fankai.db'
        self.setup_path = self.fankai_app_path / 'setup'

    def get_service_executable_path(self):
        """Retourne le chemin de l'exécutable Fankai-Service."""
        exe_name = "Fankai-Service"
        if self.current_platform == 'Windows':
            exe_name += ".exe"
        return self.setup_path / exe_name

    def ensure_dirs_exist(self):
        """Crée les répertoires nécessaires."""
        self.fankai_app_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)

def setup_logging(log_path):
    """Configure le logging pour la console et un fichier."""
    logfile = log_path / 'fankai_sync.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(logfile, 'w', 'utf-8'),
            logging.StreamHandler(sys.stdout)
        ])

    sys.excepthook = lambda exc_type, exc_value, exc_traceback: \
        logging.critical("Exception non interceptée", exc_info=(exc_type, exc_value, exc_traceback))

# --- Classes Métier ---

class DatabaseManager:
    """Gère toutes les opérations sur la base de données SQLite."""
    def __init__(self, db_path):
        self.db_path = db_path

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _base64_encode(self, name):
        return base64.b64encode(name.encode('utf-8')).decode('utf-8')

    def load_config(self):
        """Charge l'ensemble de la configuration."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM config")
            return {row[0]: row[1] for row in cursor.fetchall()}

    def update_config(self, key, value):
        """Met à jour une clé de configuration."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
            conn.commit()

    def get_folders_list(self):
        """Récupère la liste des dossiers depuis la DB."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key FROM folders")
            return [base64.b64decode(row[0]).decode('utf-8') for row in cursor.fetchall()]

    def add_folders_to_db(self, folders_to_add):
        """Ajoute une liste de dossiers à la DB."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            encoded_folders = [(self._base64_encode(folder),) for folder in folders_to_add]
            cursor.executemany("INSERT OR IGNORE INTO folders (key) VALUES (?)", encoded_folders)
            conn.commit()
            logging.info(f"{len(folders_to_add)} dossier(s) ajouté(s) à la configuration.")

class GitlabManager:
    """Gère l'interaction avec l'API GitLab."""
    def __init__(self, project_id):
        try:
            gl = gitlab.Gitlab('https://gitlab.com')
            self.project = gl.projects.get(project_id)
        except Exception as e:
            logging.error(f"Impossible de se connecter à GitLab: {e}")
            self.project = None

    def list_folders(self):
        """Liste les dossiers à la racine du projet GitLab."""
        if not self.project:
            return []
        try:
            items = self.project.repository_tree(ref='main', path='', all=True)
            return [item['name'] for item in items if item['type'] == 'tree']
        except gitlab.GitlabGetError as e:
            logging.error(f"Erreur GitLab: {e}")
            return []

class InstallerManager:
    """Gère la vérification et l'installation des dépendances comme Git."""
    @staticmethod
    def ensure_git_installed():
        """Vérifie si Git est installé et tente de l'installer sinon."""
        if shutil.which("git"):
            logging.info("Git est déjà installé.")
            return

        logging.warning("Installation de Git nécessaire...")
        os_type = platform.system()
        if os_type == 'Windows':
            git_installer_url = "https://github.com/git-for-windows/git/releases/latest/download/Git-2.43.0-64-bit.exe"
            installer_path = Path(os.getenv('TEMP', '/tmp')) / "GitInstaller.exe"
            try:
                logging.info("Téléchargement de l'installeur Git pour Windows...")
                with requests.get(git_installer_url, stream=True) as r:
                    r.raise_for_status()
                    with open(installer_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                logging.info("Lancement de l'installation de Git...")
                subprocess.run([str(installer_path), '/VERYSILENT', '/NORESTART'], check=True)
            except Exception as e:
                logging.error(f"Erreur lors de l'installation automatique de Git: {e}")
            finally:
                if installer_path.exists():
                    installer_path.unlink()
            logging.error("Veuillez installer Git pour Windows manuellement.")
        elif os_type == 'Linux':
            subprocess.run(['sudo', 'apt', 'update'], check=True)
            subprocess.run(['sudo', 'apt', 'install', 'git', '-y'], check=True)
        elif os_type == 'Darwin':
            subprocess.run(['brew', 'install', 'git'], check=True)

class SchedulerManager:
    """Gère la création et la vérification des tâches planifiées."""
    def __init__(self, config):
        self.config = config
        self.service_executable = str(config.get_service_executable_path())

    def is_task_scheduled(self):
        """Vérifie si une tâche planifiée pour Fankai existe déjà."""
        os_type = self.config.current_platform
        if os_type == 'Windows':
            task_name = "FankaiSyncHourly"
            result = subprocess.run(f'schtasks /Query /TN "{task_name}"', shell=True, capture_output=True)
            return result.returncode == 0
        elif os_type == 'Linux':
            result = subprocess.run("crontab -l | grep -F 'Fankai-Service'", shell=True, capture_output=True)
            return result.returncode == 0
        elif os_type == 'Darwin':
            result = subprocess.run(["launchctl", "list", "com.fankai.service"], capture_output=True)
            return "com.fankai.service" in result.stdout.decode()
        return False

    def schedule_task(self):
        """Crée une tâche planifiée pour exécuter le service toutes les heures."""
        os_type = self.config.current_platform
        logging.info(f"Création d'une tâche planifiée pour {os_type}...")

        if os_type == 'Windows':
            ps_script = f"""
            $Action = New-ScheduledTaskAction -Execute '{self.service_executable}'
            $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 9999)
            Register-ScheduledTask -Action $Action -Trigger $Trigger -TaskName 'FankaiSyncHourly' -Description 'Run Fankai Sync every hour' -Force
            """
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_script], check=True)
        elif os_type == 'Linux':
            cron_job = f"0 * * * * {self.service_executable} # Fankai-Service"
            subprocess.run(f'(crontab -l 2>/dev/null; echo "{cron_job}") | crontab -', shell=True, check=True)
        elif os_type == 'Darwin':
            plist_path = Path("/Library/LaunchDaemons/com.fankai.service.plist")
            plist_content = f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict>
                <key>Label</key><string>com.fankai.service</string>
                <key>Program</key><string>{self.service_executable}</string>
                <key>StartInterval</key><integer>3600</integer>
            </dict></plist>
            """
            with open(plist_path, "w") as f:
                f.write(plist_content)
            subprocess.run(["sudo", "launchctl", "load", "-w", str(plist_path)], check=True)
        
        logging.info("Tâche planifiée créée avec succès.")

class UIManager:
    """Gère l'interaction avec l'utilisateur."""
    def __init__(self, db_manager, gitlab_manager):
        self.db_manager = db_manager
        self.gitlab_manager = gitlab_manager

    def get_destination_path(self):
        """Demande et sauvegarde le chemin de destination pour la synchronisation."""
        config = self.db_manager.load_config()
        path_str = config.get('fankai_parents', 'FANKAI_PARENTS')

        if path_str != 'FANKAI_PARENTS':
            if input(f"Le chemin actuel est '{path_str}'. Est-ce correct ? (o/n) ").lower() == 'o':
                return Path(path_str)
        
        new_path = input("Veuillez entrer le nouveau chemin de destination pour les packs : ")
        self.db_manager.update_config('fankai_parents', new_path)
        return Path(new_path)

    def select_folders_to_add(self):
        """Affiche les dossiers GitLab et demande à l'utilisateur lesquels ajouter."""
        gitlab_folders = self.gitlab_manager.list_folders()
        if not gitlab_folders:
            logging.info("Aucun dossier trouvé sur GitLab.")
            return

        existing_folders = self.db_manager.get_folders_list()
        new_folders = sorted([f for f in gitlab_folders if f not in existing_folders])

        if not new_folders:
            logging.info("Tous les dossiers de GitLab sont déjà configurés.")
            return

        print("\nDossiers disponibles sur GitLab à ajouter :")
        for i, folder in enumerate(new_folders, 1):
            print(f"  {i}. {folder}")

        user_input = input("Entrez les numéros des dossiers à ajouter (ex: 1, 3, 5), 'all' pour tous, ou Entrée pour ignorer: ")
        
        folders_to_add = []
        if user_input.lower() == 'all':
            folders_to_add = new_folders
        elif user_input:
            try:
                indices = [int(idx.strip()) - 1 for idx in user_input.split(',')]
                folders_to_add = [new_folders[i] for i in indices if 0 <= i < len(new_folders)]
            except ValueError:
                logging.error("Entrée invalide.")

        if folders_to_add:
            self.db_manager.add_folders_to_db(folders_to_add)

class Application:
    """Classe principale qui orchestre le script."""
    def __init__(self):
        self.config = Config()
        self.db_manager = DatabaseManager(self.config.db_path)
        self.gitlab_manager = GitlabManager(self.config.gitlab_project_id)
        self.installer_manager = InstallerManager()
        self.scheduler_manager = SchedulerManager(self.config)
        self.ui_manager = UIManager(self.db_manager, self.gitlab_manager)

    def run(self):
        """Exécute le processus complet de configuration et de synchronisation."""
        self.config.ensure_dirs_exist()
        os.chdir(self.config.fankai_app_path)
        setup_logging(self.config.log_path)
        
        print(pyfiglet.figlet_format("FANKAI-SYNC"))
        
        self.installer_manager.ensure_git_installed()
        destination_path = self.ui_manager.get_destination_path()
        self.ui_manager.select_folders_to_add()

        # Lancer le service une première fois
        self.run_service_once()
        
        # Gérer la planification
        if not self.scheduler_manager.is_task_scheduled():
            if input("\nVoulez-vous planifier la synchronisation automatique toutes les heures ? (o/n) ").lower() == 'o':
                try:
                    self.scheduler_manager.schedule_task()
                except Exception as e:
                    logging.error(f"Échec de la création de la tâche planifiée : {e}")
                    logging.error("Veuillez essayer de lancer ce script avec des privilèges administrateur/sudo.")
        else:
            logging.info("Une tâche de synchronisation automatique est déjà en place.")

        logging.info("Configuration de la synchronisation terminée.")

    def run_service_once(self):
        """Lance l'exécutable Fankai-Service une fois."""
        logging.info("Lancement de la synchronisation initiale...")
        service_path = self.config.get_service_executable_path()
        if not service_path.exists():
            logging.error(f"L'exécutable du service '{service_path.name}' est introuvable. Veuillez d'abord lancer Fankai-All.")
            return

        try:
            if platform.system() != 'Windows':
                 service_path.chmod(service_path.stat().st_mode | 0o777)
            subprocess.run([str(service_path)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.error(f"Erreur lors de l'exécution du service de synchronisation : {e}")


# --- Point d'entrée ---

def handle_interrupt(sig, frame):
    print("\nSignal d'interruption reçu. Au revoir !")
    sys.exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        app = Application()
        app.run()
    except Exception as e:
        logging.critical(f"Une erreur fatale est survenue: {e}", exc_info=True)
    
    input("\nAppuyez sur Entrée pour quitter.")

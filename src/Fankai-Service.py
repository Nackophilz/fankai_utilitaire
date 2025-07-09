# -*- coding: utf-8 -*-


import base64
import logging
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, Repo

# --- Configuration & Helpers ---

class Config:
    """
    Centralise la configuration, les chemins et la logique dépendante de la plateforme.
    """
    def __init__(self):
        self.gitlab_project_id = "ElPouki/fankai_synchro"
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
    
    def get_sync_destination(self, db_config):
        """Récupère le chemin de destination de la synchronisation depuis la config DB."""
        path_str = db_config.get('fankai_parents')
        if not path_str or path_str == "FANKAI_PARENTS":
            raise ValueError("Le chemin de destination 'fankai_parents' n'est pas configuré dans la base de données.")
        return Path(path_str)

    def ensure_dirs_exist(self):
        """Crée les répertoires nécessaires."""
        self.fankai_app_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)


def setup_logging(log_path):
    """Configure le logging pour la console et un fichier."""
    logfile = log_path / 'fankai_service.log'
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

    def _base64_decode(self, encoded_name):
        return base64.b64decode(encoded_name.encode('utf-8')).decode('utf-8')

    def load_config(self):
        """Charge l'ensemble de la configuration depuis la base de données."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM config ORDER BY key")
            return {row[0]: row[1] for row in cursor.fetchall()}

    def get_folders_to_sync(self):
        """Récupère la liste des dossiers à synchroniser depuis la DB."""
        logging.info("Récupération de la liste des dossiers à synchroniser...")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key FROM folders ORDER BY key")
            return [self._base64_decode(row[0]) for row in cursor.fetchall()]

    def update_local_folders_in_db(self, destination_path):
        """Met à jour la DB avec les dossiers présents localement."""
        logging.info("Mise à jour de la base de données avec les dossiers locaux...")
        destination_path.mkdir(parents=True, exist_ok=True)
        
        try:
            local_folders = [item.name for item in destination_path.iterdir() if item.is_dir()]
        except OSError as e:
            logging.error(f"Erreur lors du scan de '{destination_path}': {e}")
            return

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("ALTER TABLE folders ADD COLUMN seen INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass # La colonne existe déjà

            for folder_name in local_folders:
                encoded_name = self._base64_encode(folder_name)
                cursor.execute(
                    "INSERT INTO folders (key, seen) VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET seen = 1",
                    (encoded_name,)
                )
            
            # Suppression des dossiers qui ne sont plus présents localement
            encoded_local_folders = [self._base64_encode(name) for name in local_folders]
            if encoded_local_folders:
                placeholders = ','.join(['?'] * len(encoded_local_folders))
                cursor.execute(f"DELETE FROM folders WHERE key NOT IN ({placeholders}) AND seen = 1", encoded_local_folders)
            
            conn.commit()

class GitManager:
    """Gère toutes les interactions avec le dépôt Git."""
    def __init__(self, repo_path, project_id):
        self.repo_path = repo_path.resolve()
        self.repo_url = f"https://gitlab.com/{project_id}.git"
        self.repo = None

    def _ensure_safe_directory(self):
        """Vérifie et ajoute le chemin du dépôt aux répertoires sûrs de Git."""
        try:
            result = subprocess.run(
                ['git', 'config', '--global', '--get-all', 'safe.directory'],
                capture_output=True, text=True
            )
            safe_directories = result.stdout.splitlines()
            if str(self.repo_path) in safe_directories:
                return

            subprocess.run(
                ['git', 'config', '--global', '--add', 'safe.directory', str(self.repo_path)],
                check=True
            )
            logging.info(f"Ajouté aux répertoires Git sûrs : {self.repo_path}")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.error(f"Impossible de configurer le répertoire Git sûr : {e}")

    def _init_or_load_repo(self):
        """Initialise un nouveau dépôt ou charge un dépôt existant."""
        self._ensure_safe_directory()
        try:
            self.repo = Repo(self.repo_path)
            logging.info("Dépôt local trouvé.")
            if 'origin' not in self.repo.remotes:
                 self.repo.create_remote('origin', self.repo_url)
            else:
                 self.repo.remotes.origin.set_url(self.repo_url)
        except InvalidGitRepositoryError:
            logging.info("Initialisation d'un nouveau dépôt local...")
            self.repo = Repo.init(self.repo_path)
            self.repo.create_remote('origin', self.repo_url)

    def sync(self, folders_list):
        """Configure le sparse-checkout et synchronise le dépôt."""
        self._init_or_load_repo()
        
        logging.info("Configuration du sparse-checkout...")
        sparse_checkout_file = self.repo_path / ".git" / "info" / "sparse-checkout"
        sparse_checkout_file.parent.mkdir(exist_ok=True)
        
        with self.repo.config_writer() as config:
            config.set_value('core', 'sparseCheckout', 'true')
        
        with open(sparse_checkout_file, 'w', encoding='utf-8') as f:
            f.write("/*\n!*/*/\n")
            f.write('\n'.join(folders_list) + '\n')
            
        try:
            logging.info("Récupération des données depuis le dépôt distant (fetch)...")
            self.repo.remotes.origin.fetch()
            
            logging.info("Réinitialisation du dépôt local à l'état de 'origin/main' (reset --hard)...")
            self.repo.git.reset('--hard', 'origin/main')
            logging.info("Synchronisation terminée avec succès.")
        except GitCommandError as e:
            logging.error(f"Erreur Git lors de la synchronisation : {e}")

class Application:
    """Classe principale qui orchestre la synchronisation."""
    def __init__(self):
        self.config = Config()
        self.db_manager = DatabaseManager(self.config.db_path)
    
    def run(self):
        """Exécute le processus complet de synchronisation."""
        self.config.ensure_dirs_exist()
        os.chdir(self.config.fankai_app_path)
        setup_logging(self.config.log_path)
        
        logging.info("Démarrage du service de synchronisation Fankai.")
        
        try:
            db_config = self.db_manager.load_config()
            destination_path = self.config.get_sync_destination(db_config)

            self.db_manager.update_local_folders_in_db(destination_path)
            
            folders_to_sync = self.db_manager.get_folders_to_sync()
            if not folders_to_sync:
                logging.info("Aucun dossier à synchroniser. Le service a terminé.")
                return

            git_manager = GitManager(destination_path, self.config.gitlab_project_id)
            git_manager.sync(folders_to_sync)
            
        except Exception as e:
            logging.critical(f"Une erreur critique est survenue: {e}", exc_info=True)

if __name__ == '__main__':
    try:
        app = Application()
        app.run()
    except Exception as e:
        logging.critical(f"Erreur fatale lors de l'initialisation: {e}", exc_info=True)

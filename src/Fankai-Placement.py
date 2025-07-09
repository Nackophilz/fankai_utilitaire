# -*- coding: utf-8 -*-


import argparse
import logging
import os
import platform
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from tkinter import Tk, filedialog

import pyfiglet
import requests
import urllib3
from github import Github
from rapidfuzz import fuzz, process
from tqdm import tqdm

# --- Configuration & Helpers ---

# Désactiver les avertissements de certificat SSL (si nécessaire)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class Config:
    """
    Centralise la configuration, les chemins et la logique dépendante de la plateforme.
    """
    def __init__(self):
        self.current_platform = platform.system()
        self.api_base_url = "https://metadata.fankai.fr"
        self.github_repo = "Nackophilz/fankai_utilitaire"

        self._configure_paths_and_settings()

    def _configure_paths_and_settings(self):
        """Définit les chemins et paramètres spécifiques à l'OS."""
        if self.current_platform == 'Windows':
            self.app_data_path = Path(os.getenv('APPDATA', ''))
            self.metadata_executable = 'Fankai-Metadata.exe'
        elif self.current_platform == 'Linux':
            self.app_data_path = Path(os.path.expanduser('~/.local/share'))
            self.metadata_executable = 'Fankai-Metadata'
        elif self.current_platform == 'Darwin': # macOS
            self.app_data_path = Path.home() / "Library" / "Application Support"
            self.metadata_executable = 'Fankai-Metadata'
        else:
            raise Exception(f"OS non supporté: {self.current_platform}")

        self.fankai_app_path = self.app_data_path / 'fankai'
        self.log_path = self.fankai_app_path / 'logs'
        self.db_path = self.fankai_app_path / 'fankai.db'
        self.metadata_script_path = self.fankai_app_path / 'setup' / self.metadata_executable

    def ensure_dirs_exist(self):
        """Crée les répertoires nécessaires."""
        self.fankai_app_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)
        (self.fankai_app_path / 'setup').mkdir(parents=True, exist_ok=True)

def setup_logging(log_path):
    """Configure le logging pour la console et un fichier."""
    logfile = log_path / 'fankai_placement.log'
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

def clear_host():
    """Nettoie la console."""
    os.system('cls' if platform.system() == 'Windows' else 'clear')

def handle_interrupt(sig, frame):
    """Gère le signal d'interruption (Ctrl+C)."""
    logging.info("\nSignal d'interruption reçu. Au revoir !")
    sys.exit(0)

# --- Classes Métier ---

class DatabaseManager:
    """Gère toutes les opérations sur la base de données SQLite."""
    def __init__(self, db_path):
        self.db_path = db_path
        self._ensure_db_structure()

    def _ensure_db_structure(self):
        """S'assure que les tables nécessaires existent."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS rename (
                    line TEXT PRIMARY KEY
                )
            ''')
            # Initialisation des valeurs par défaut si elles n'existent pas
            default_config = {
                "fankai_parents": "FANKAI_PARENTS", "fankai_telechargement": "FANKAI_TELECHARGEMENT",
                "plex_plugin": "PLEX_PLUGIN", "type_placement": "TYPE_PLACEMENT"
            }
            cursor.executemany("INSERT OR IGNORE INTO config(key, value) VALUES(?, ?)", default_config.items())
            conn.commit()

    def load_config(self):
        """Charge l'ensemble de la configuration depuis la base de données."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM config")
                return {row[0]: row[1] for row in cursor.fetchall()}
        except sqlite3.Error as e:
            logging.error(f"Erreur de base de données lors du chargement de la config: {e}")
            return {}

    def update_config(self, config_data):
        """Met à jour une ou plusieurs clés dans la configuration."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("REPLACE INTO config (key, value) VALUES (?, ?)", config_data.items())
            conn.commit()

    def load_rename_dict(self):
        """Charge la liste de renommage depuis la DB et la retourne comme un dictionnaire."""
        rename_dict = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT line FROM rename")
                for row in cursor.fetchall():
                    parts = row[0].split(" -> ")
                    if len(parts) == 2:
                        rename_dict[parts[0].strip()] = parts[1].strip()
        except sqlite3.Error as e:
            logging.error(f"Erreur DB lors du chargement de la liste de renommage : {e}")
        return rename_dict

    def update_rename_list_from_github(self, repo_name):
        """Met à jour la table de renommage depuis le fichier films.txt sur GitHub."""
        logging.info("Recherche de mise à jour pour la liste de renommage...")
        try:
            g = Github()
            repo = g.get_repo(repo_name)
            contents = repo.get_contents("rename/films.txt")
            response = requests.get(contents.download_url)
            response.raise_for_status()
            remote_lines = set(line.strip() for line in response.text.splitlines() if "->" in line)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT line FROM rename")
                local_lines = set(row[0] for row in cursor.fetchall())

                lines_to_add = remote_lines - local_lines
                lines_to_remove = local_lines - remote_lines

                if lines_to_add:
                    cursor.executemany("INSERT OR IGNORE INTO rename (line) VALUES (?)", [(line,) for line in lines_to_add])
                if lines_to_remove:
                    cursor.executemany("DELETE FROM rename WHERE line = ?", [(line,) for line in lines_to_remove])
                
                conn.commit()

                if lines_to_add or lines_to_remove:
                    logging.info("La liste de renommage a été mise à jour.")
                else:
                    logging.info("La liste de renommage est déjà à jour.")

        except Exception as e:
            logging.error(f"Impossible de mettre à jour la liste de renommage depuis GitHub : {e}")

class FileSystemManager:
    """Gère les interactions avec le système de fichiers."""
    
    def collect_video_files(self, directory):
        """Scan un répertoire de manière récursive pour les fichiers vidéo."""
        video_files = []
        allowed_extensions = (".mkv", ".mp4")
        logging.info(f"Scan des fichiers vidéo dans : {directory}...")
        for root, _, files in os.walk(directory):
            for file in files:
                if file.lower().endswith(allowed_extensions):
                    video_files.append(os.path.join(root, file))
        return video_files

    def list_nfo_files(self, directory):
        """Scan un répertoire de manière récursive pour les fichiers .nfo."""
        nfo_files = []
        logging.info(f"Scan des fichiers NFO en local dans : {directory}...")
        for root, _, files in os.walk(directory):
            for file in files:
                if file.lower().endswith(".nfo"):
                    nfo_files.append(os.path.join(root, file))
        return nfo_files

    def get_nfo_files_from_api(self, media_path, api_base_url):
        """Récupère la liste des chemins de fichiers NFO depuis l'API Fankai."""
        nfo_files = []
        api_endpoint = f"{api_base_url}/episodes/infos"
        logging.info(f"Récupération des fichiers NFO depuis l'API : {api_endpoint}...")
        try:
            response = requests.get(api_endpoint)
            response.raise_for_status()
            data = response.json()
            for item in data:
                if 'nfo_path' in item and item['nfo_path']:
                    full_path = os.path.join(media_path, item['nfo_path'])
                    nfo_files.append(full_path)
        except requests.RequestException as e:
            logging.error(f"Erreur lors de la récupération des NFO depuis l'API : {e}")
        return nfo_files

    def create_atomic_link(self, source, destination):
        """Crée un hardlink en créant les répertoires parents."""
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            os.link(source, destination)
        except OSError as e:
            logging.error(f"Erreur lors de la création du hardlink pour {source} -> {destination} : {e}")
            if platform.system() == 'Windows':
                try:
                    subprocess.run(['fsutil', 'hardlink', 'create', destination, source], check=True, capture_output=True)
                except subprocess.CalledProcessError as sub_e:
                    logging.error(f"fsutil a échoué : {sub_e.stderr.decode('cp850', errors='ignore')}")

    def copy_file(self, source, destination):
        """Copie un fichier, en créant les répertoires nécessaires."""
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)

    def launch_metadata_script(self, script_path, series_list):
        """Lance le script Fankai-Metadata avec les séries concernées."""
        if not series_list:
            logging.info("Aucune nouvelle série à traiter, Fankai-Metadata ne sera pas lancé.")
            return

        if not script_path.exists():
            logging.error(f"L'exécutable Fankai-Metadata est introuvable : {script_path}")
            return
        
        series_arg = ",".join(series_list)
        logging.info(f"Lancement de Fankai-Metadata pour les séries : {series_arg}")
        try:
            subprocess.run([str(script_path), "--series", series_arg], check=True)
            logging.info("Fankai-Metadata s'est terminé avec succès.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Erreur lors de l'exécution de Fankai-Metadata : {e}")
        except Exception as e:
            logging.error(f"Erreur inattendue au lancement de Fankai-Metadata : {e}")


class FileMatcher:
    """Logique de correspondance entre les fichiers vidéo et NFO."""
    

    def find_matches(self, video_files, nfo_files, rename_dict, threshold=95):
        """
        Trouve les correspondances. Utilise une logique spéciale pour One Piece
        afin de trouver toutes les destinations possibles, et non juste la meilleure.
        """
        standard_matches = []
        unmatched = []
        one_piece_matches = {} 
        
        # Prépare les dictionnaires de NFO pour la recherche
        self.nfo_basenames = {os.path.splitext(os.path.basename(nfo))[0]: nfo for nfo in nfo_files}
        
        # Sépare les NFO de One Piece pour optimiser la recherche
        op_nfo_choices = {k: v for k, v in self.nfo_basenames.items() if 'one piece' in k.lower()}
        non_op_nfo_choices = {k: v for k, v in self.nfo_basenames.items() if 'one piece' not in k.lower()}

        for video in tqdm(video_files, desc="Analyse des fichiers"):
            video_basename = os.path.splitext(os.path.basename(video))[0]
            found_match = False

            # 1. Correspondance exacte via le dictionnaire de renommage
            if video_basename in rename_dict:
                nfo_target_name = rename_dict[video_basename]
                if nfo_target_name in self.nfo_basenames:
                    standard_matches.append((video, self.nfo_basenames[nfo_target_name], 101))
                    found_match = True

            # 2. Si le fichier est un "One Piece"
            elif 'one piece' in video_basename.lower():
                # Cherche TOUTES les correspondances possibles dépassant le seuil
                possible_matches = process.extract(video_basename, op_nfo_choices.keys(), scorer=fuzz.ratio, score_cutoff=threshold)
                
                if possible_matches:
                    # Stocke toutes les destinations valides pour ce fichier vidéo
                    one_piece_matches[video] = [(op_nfo_choices[match[0]], match[1]) for match in possible_matches]
                    found_match = True

            # 3. Correspondance basique
            if not found_match:
                best_match = process.extractOne(video_basename, non_op_nfo_choices.keys(), scorer=fuzz.ratio)
                if best_match and best_match[1] >= threshold:
                    matched_nfo_name = best_match[0]
                    standard_matches.append((video, non_op_nfo_choices[matched_nfo_name], best_match[1]))
                    found_match = True

            if not found_match:
                # Si aucune correspondance, cherche des suggestions dans TOUS les NFO
                suggestions = process.extract(video_basename, self.nfo_basenames.keys(), scorer=fuzz.ratio, limit=5)
                unmatched.append((video, suggestions))

        # Retourne 3 listes: les matchs standards, les non-matchés, et les matchs multiples de One Piece
        return standard_matches, unmatched, one_piece_matches


class FilePlacer:
    """Contient la logique pour copier ou créer des hardlinks pour les fichiers."""
    def __init__(self, fs_manager, db_manager, ui_manager, is_auto_mode):
        self.fs = fs_manager
        self.db = db_manager
        self.ui = ui_manager
        self.is_auto = is_auto_mode


    def place_files(self, standard_matches, op_matches, placement_mode, media_root):
        """
        Traite tous les fichiers. Gère les matchs standards (1-pour-1) et les 
        matchs One Piece (1-pour-plusieurs). Permet le remplacement de fichiers.
        """
        placed_files = []
        updated_series = set()
        action_verb = "Copie" if placement_mode == 'c' else "Création de lien"
        

        all_matches = op_matches.copy()
        for video, nfo, score in standard_matches:
            if video not in all_matches:
                all_matches[video] = []
            all_matches[video].append((nfo, score))


        for video, nfo_options in tqdm(all_matches.items(), desc=f"{action_verb} en cours"):
            is_op_video = 'one piece' in os.path.basename(video).lower()
            is_yabai_release = "yabai" in os.path.basename(video).lower()

            # Pour chaque vidéo, on parcourt toutes ses destinations possibles
            for nfo, score in nfo_options:
                # --- LOGIQUE SPÉCIFIQUE POUR ONE PIECE ---
                if is_op_video:
                    is_yabai_destination = "one piece yabai" in nfo.lower()
                    if is_yabai_destination and not is_yabai_release:
                        logging.warning(f"Placement ignoré : Le fichier non-Yabai '{os.path.basename(video)}' ne peut pas aller dans la destination Yabai '{os.path.relpath(nfo, media_root)}'.")
                        continue
                
                nfo_basename = os.path.splitext(os.path.basename(nfo))[0]
                destination_folder = os.path.dirname(nfo)
                extension = os.path.splitext(video)[1]
                destination_file = os.path.join(destination_folder, f"{nfo_basename}{extension}")

                try:
                    # Si le fichier existe déjà, on l'ignore et passe au suivant.
                    if os.path.exists(destination_file):
                        continue # On passe à la destination/au fichier suivant(e)
                    
                    logging.info(f"{action_verb} pour {os.path.basename(video)} -> {os.path.relpath(destination_file, media_root)} (Score: {score:.0f})")
                    if placement_mode == 'c':
                        self.fs.copy_file(video, destination_file)
                    else:
                        self.fs.create_atomic_link(video, destination_file)
                    
                    # On ajoute le fichier à la liste des fichiers placés
                    placed_files.append(video)
                    
                    # On extrait le nom de la série pour la mise à jour finale des métadonnées
                    serie_folder = Path(nfo).relative_to(media_root).parts[0]
                    updated_series.add(serie_folder)

                except Exception as e:
                    logging.error(f"Erreur lors du placement de {os.path.basename(video)} vers {destination_file}: {e}")


        return list(set(placed_files)), updated_series



class UIManager:
    """Gère toutes les interactions avec l'utilisateur (prompts, menus)."""
    def __init__(self, db_manager, is_auto_mode):
        self.db = db_manager
        self.is_auto = is_auto_mode
    
    def display_intro(self):
        """Affiche la bannière et le message d'introduction."""
        clear_host()
        print(pyfiglet.figlet_format("FANKAI-MOVE"))
        logging.info("Bienvenue sur l'outil de placement de fichiers Fankai.")
        logging.info("Ce script va scanner vos téléchargements et les placer dans votre médiathèque.")
        if self.is_auto:
            logging.info("Mode automatique activé.")
        time.sleep(2)

    def ask_for_directory(self, prompt):
        """Demande un répertoire à l'utilisateur via GUI ou CLI."""
        if not self.is_auto:
            try:
                root = Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(title=prompt)
                root.destroy()
                if path:
                    return path
            except Exception:
                logging.warning("L'interface graphique n'a pas pu se lancer, passage en mode console.")
            return input(f"{prompt} : ")
        return None

    def get_paths(self):
        """Gère la configuration des chemins source et destination."""
        config = self.db.load_config()
        media_path = config.get("fankai_parents")
        download_path = config.get("fankai_telechargement")

        is_media_ok = media_path and media_path != "FANKAI_PARENTS"
        is_download_ok = download_path and download_path != "FANKAI_TELECHARGEMENT"

        if self.is_auto:
            if not is_media_ok or not is_download_ok:
                logging.error("Les chemins média et/ou téléchargement ne sont pas configurés. Impossible de continuer en mode auto.")
                sys.exit(1)
            return {"media": media_path, "download": download_path}

        clear_host()
        logging.info("Configuration des chemins de travail.")

        if not self.ask_yes_no(f"Chemin de la médiathèque : {media_path}\nEst-ce correct ?", default_yes=is_media_ok):
            media_path = self.ask_for_directory("Veuillez sélectionner le dossier parent de votre médiathèque Kaï")
            self.db.update_config({"fankai_parents": media_path})

        if not self.ask_yes_no(f"Chemin des téléchargements : {download_path}\nEst-ce correct ?", default_yes=is_download_ok):
            download_path = self.ask_for_directory("Veuillez sélectionner le dossier contenant vos téléchargements Kaï")
            self.db.update_config({"fankai_telechargement": download_path})
        
        return {"media": media_path, "download": download_path}

    def get_placement_method(self):
        """Demande à l'utilisateur de choisir entre Hardlink et Copie."""
        config = self.db.load_config()
        choice = config.get("type_placement")

        if self.is_auto:
            if choice not in ['h', 'c']:
                logging.error("Le type de placement (h/c) n'est pas configuré. Impossible de continuer en mode auto.")
                sys.exit(1)
            return choice

        if choice in ['h', 'c']:
            action = "Hardlink" if choice == "h" else "Copie"
            if self.ask_with_timeout(f"Le mode '{action}' est pré-configuré. Appuyez sur une touche pour changer.", 5):
                choice = None # Forcer un nouveau choix

        while choice not in ['h', 'c']:
            clear_host()
            user_input = input("Quelle méthode utiliser ? (H: Hardlink, C: Copie, ?: Aide) : ").lower()
            if user_input == '?':
                self.display_help()
            elif user_input in ['h', 'c']:
                choice = user_input
        
        self.db.update_config({"type_placement": choice})
        return choice
    
    def confirm_plex_usage(self):
        """Demande à l'utilisateur s'il utilise le plugin Plex si ce n'est pas déjà configuré."""
        config = self.db.load_config()
        plex_plugin_setting = config.get("plex_plugin")

        if self.is_auto:
            if plex_plugin_setting not in ['y', 'n']:
                logging.error("Le paramètre du plugin Plex (y/n) n'est pas configuré. Impossible de continuer en mode auto.")
                sys.exit(1)
            return plex_plugin_setting == 'y'

        if plex_plugin_setting == "PLEX_PLUGIN" or plex_plugin_setting is None:
             if self.ask_yes_no("Utilisez-vous l'intégration Plex (récupération des données via l'API) ?"):
                 self.db.update_config({'plex_plugin': 'y'})
                 return True
             else:
                 self.db.update_config({'plex_plugin': 'n'})
                 return False
        
        return plex_plugin_setting == 'y'
            

    def display_help(self):
        """Affiche l'aide pour les méthodes de placement."""
        clear_host()
        logging.info("=" * 50)
        logging.info("Explication des méthodes de placement")
        logging.info("=" * 50)
        logging.info("\nCopie (C) :")
        logging.info("- Duplique vos fichiers téléchargés dans votre médiathèque.")
        logging.info("- Vous pouvez ensuite supprimer les fichiers d'origine.")
        logging.info("- Utilise deux fois l'espace disque (temporairement).")
        logging.info("\nHardlink (H) :")
        logging.info("- Crée un 'miroir' du fichier dans votre médiathèque.")
        logging.info("- Le fichier existe à deux endroits mais n'occupe l'espace disque qu'une seule fois.")
        logging.info("- Idéal pour continuer à partager (seed) après le placement.")
        logging.info("- REQUIERT que les dossiers de téléchargement et de médiathèque soient sur le MÊME volume/disque.")
        logging.info("=" * 50)
        self.pause()

    def handle_unmatched(self, unmatched_files, nfo_files, placement_mode, media_root):
        if self.is_auto or not unmatched_files:
            return
        
        logging.info("\n--- Traitement manuel des fichiers sans correspondance ---")
        nfo_basenames = {os.path.splitext(os.path.basename(nfo))[0]: nfo for nfo in nfo_files}
        
        for video, suggestions in unmatched_files:
            if re.search(r'opening|ending|creditless|op\d*|ed\d*', os.path.basename(video), re.I):
                continue
            
            print("\n" + "="*20)
            logging.info(f"Fichier sans correspondance : {os.path.basename(video)}")
            
            if not suggestions:
                logging.warning("Aucune suggestion trouvée.")
                continue

            for i, (name, score, _) in enumerate(suggestions):
                logging.info(f"  {i+1}. {name} (Score: {score:.0f})")

            try:
                choice = input("Choisissez une correspondance (1-5) ou Entrée pour ignorer : ")
                if not choice or not choice.isdigit() or not 1 <= int(choice) <= len(suggestions):
                    logging.info("Ignoré.")
                    continue
                
                selected_nfo_name = suggestions[int(choice) - 1][0]
                nfo = nfo_basenames[selected_nfo_name]
                

                placer = FilePlacer(FileSystemManager(), self.db, self, self.is_auto)
                placer.place_files([], {video: [(nfo, 102)]}, placement_mode, media_root)


            except (ValueError, IndexError):
                logging.info("Choix invalide, ignoré.")

    def ask_yes_no(self, prompt, default_yes=False):
        """Pose une question Oui/Non."""
        if self.is_auto: return default_yes
        suffix = " (Y/n)" if default_yes else " (y/N)"
        while True:
            response = input(f"{prompt}{suffix} ").lower().strip()
            if response == 'y': return True
            if response == 'n': return False
            if response == '': return default_yes
            
    def confirm_cleanup(self, placed_files):
        """Demande si l'utilisateur veut supprimer les fichiers source après copie."""
        if not self.is_auto and self.ask_yes_no("\nVoulez-vous supprimer les fichiers originaux qui ont été copiés ?"):
            logging.info("Suppression des fichiers source...")
            for f in tqdm(list(set(placed_files)), desc="Nettoyage"):
                try:
                    os.remove(f)
                except Exception as e:
                    logging.error(f"Impossible de supprimer {f}: {e}")
                    
    def pause(self):
        if not self.is_auto: input("\nAppuyez sur Entrée pour continuer...")

    def ask_with_timeout(self, prompt, timeout=5):
        """Attend une touche pendant un temps donné. Multi-plateforme."""
        if self.is_auto:
            return False
        
        print(prompt, end="", flush=True)
        
        key_pressed = False
        if platform.system() == "Windows":
            import msvcrt
            start_time = time.time()
            while time.time() - start_time < timeout:
                if msvcrt.kbhit():
                    msvcrt.getch()  # Consomme la touche
                    key_pressed = True
                    break
                time.sleep(0.05)
        else:
            rlist, _, _ = select.select([sys.stdin], [], [], timeout)
            if rlist:
                sys.stdin.readline()
                key_pressed = True
        
        print()
        return key_pressed

class Application:
    """Classe principale qui orchestre l'application."""
    def __init__(self):
        self.args = self._parse_arguments()
        self.config = Config()
        self.db = DatabaseManager(self.config.db_path)
        self.fs = FileSystemManager()
        self.matcher = FileMatcher()
        self.ui = UIManager(self.db, self.args.auto)
        self.placer = FilePlacer(self.fs, self.db, self.ui, self.args.auto)
        
    def _parse_arguments(self):
        """Gère les arguments de la ligne de commande."""
        parser = argparse.ArgumentParser(description="Fankai-Move: Placement de fichiers automatisé.")
        parser.add_argument("auto", nargs='?', help="Lance le script en mode automatique non-interactif.")
        return parser.parse_args()

    def run(self):
        """Point d'entrée principal de l'application."""
        self.config.ensure_dirs_exist()
        os.chdir(self.config.fankai_app_path)
        setup_logging(self.config.log_path)
        
        self.ui.display_intro()
        
        self.db.update_rename_list_from_github(self.config.github_repo)
        
        paths = self.ui.get_paths()
        media_path, download_path = paths['media'], paths['download']
        
        placement_method = self.ui.get_placement_method()
        
        clear_host()
        logging.info("Préparation de l'analyse...")
        
        video_files = self.fs.collect_video_files(download_path)

        plex_enabled = self.ui.confirm_plex_usage()
        if plex_enabled:
            nfo_files = self.fs.get_nfo_files_from_api(media_path, self.config.api_base_url)
        else:
            nfo_files = self.fs.list_nfo_files(media_path)
        
        if not video_files:
            logging.warning("Aucun fichier vidéo trouvé dans le dossier de téléchargement. Fin du script.")
            return

        if not nfo_files:
            logging.warning("Aucun fichier NFO de référence trouvé (localement ou via l'API). Impossible de trouver des correspondances.")
            return
            
        rename_dict = self.db.load_rename_dict()

        standard_matches, unmatched, op_matches = self.matcher.find_matches(video_files, nfo_files, rename_dict)

        if not standard_matches and not op_matches and not unmatched:
             logging.warning("Aucune correspondance trouvée pour les fichiers vidéo.")
        else:
            placed_files, updated_series = self.placer.place_files(standard_matches, op_matches, placement_method, media_path)
            
            if placed_files:
                logging.info(f"\nPlacement terminé. {len(placed_files)} fichier(s) source traité(s).")
                logging.info(f"Séries mises à jour : {', '.join(updated_series) or 'Aucune'}")
                
                if placement_method == 'c':
                    self.ui.confirm_cleanup(placed_files)

                if self.ui.ask_yes_no("Lancer Fankai-Metadata pour mettre à jour les métadonnées ?", default_yes=self.args.auto):
                    self.fs.launch_metadata_script(self.config.metadata_script_path, list(updated_series))
            else:
                logging.info("Aucun nouveau fichier n'a été placé.")

        self.ui.handle_unmatched(unmatched, nfo_files, placement_method, media_path)

        logging.info("\nScript terminé.")

# --- Point d'entrée ---

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        app = Application()
        app.run()
    except Exception as e:
        logging.critical(f"Une erreur fatale est survenue: {e}", exc_info=True)
    
    if len(sys.argv) <= 1 or sys.argv[1].lower() != 'auto':
        input("\nAppuyez sur Entrée pour quitter.")

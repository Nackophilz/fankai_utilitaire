# -*- coding: utf-8 -*-

import argparse
import logging
import os
import platform
import signal
import sqlite3
import sys
import time
from getpass import getpass
from pathlib import Path

import pyfiglet
import requests
import urllib3
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
        self.api_base_url = "https://metadata.fankai.fr"
        
        self._configure_paths_and_settings()

    def _configure_paths_and_settings(self):
        """Définit les chemins et paramètres spécifiques à l'OS."""
        if self.current_platform == 'Windows':
            self.app_data_path = Path(os.getenv('APPDATA', ''))
            self.verify_ssl = True
        elif self.current_platform == 'Linux':
            self.app_data_path = Path(os.path.expanduser('~/.local/share'))
            self.verify_ssl = True
        elif self.current_platform == 'Darwin':
            self.app_data_path = Path.home() / "Library" / "Application Support"
            self.verify_ssl = False
        else:
            raise Exception(f"OS non supporté: {self.current_platform}")

        self.fankai_app_path = self.app_data_path / 'fankai'
        self.log_path = self.fankai_app_path / 'logs'
        self.db_path = self.fankai_app_path / 'fankai.db'

    def ensure_dirs_exist(self):
        """Crée les répertoires nécessaires."""
        self.fankai_app_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)

def setup_logging(log_path):
    """Configure le logging pour la console et un fichier."""
    logfile = log_path / 'fankai_metadata.log'
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

    def load_config(self):
        """Charge l'ensemble de la configuration depuis la base de données."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM config")
                return {row[0]: row[1] for row in cursor.fetchall()}
        except sqlite3.Error as e:
            logging.error(f"Erreur de base de données: {e}")
            return {}

    def update_config(self, config_data):
        """Met à jour une ou plusieurs clés dans la configuration."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("REPLACE INTO config (key, value) VALUES (?, ?)", config_data.items())
            conn.commit()

class PlexManager:
    """Gère l'authentification et l'interaction avec le serveur Plex."""
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.plex_server = None

    def connect(self):
        """Tente de se connecter au serveur Plex et gère la reconfiguration si nécessaire."""
        while not self._try_connect():
            if input("La connexion a échoué. Voulez-vous reconfigurer les identifiants ? (o/n) ").lower() == 'o':
                self._gather_and_save_plex_credentials()
            else:
                return False
        return True

    def _try_connect(self):
        """Tente de se connecter au serveur Plex en utilisant les URL stockées."""
        config = self.db_manager.load_config()
        token = config.get("plex_token")
        urls = [config.get("plex_ip_locale"), config.get("plex_ip_publique")]

        if not token or token == "TOKEN_PLEX":
            return False

        for url in filter(None, urls):
            try:
                self.plex_server = PlexServer(url, token)
                logging.info(f"Connecté avec succès à Plex via {url}")
                return True
            except Exception:
                logging.warning(f"Échec de la connexion à {url}, essai suivant...")
        return False

    def _gather_and_save_plex_credentials(self):
        """Récupère et sauvegarde les informations de connexion Plex."""
        clear_host()
        logging.info("Configuration de la connexion à votre compte Plex.")
        
        max_attempts = 3
        for attempt in range(max_attempts):
            username = input("Adresse e-mail Plex : ")
            password = getpass("Mot de passe Plex : ")
            
            try:
                account = MyPlexAccount(username, password)
            except TwoFactorRequired:
                code = input("Code 2FA Plex : ")
                account = MyPlexAccount(username, password, code=code)
            except Exception as e:
                logging.error(f"Échec de l'authentification Plex: {e}")
                continue
            
            local_uri, remote_uri = self._select_server_and_connections(account)
            self.db_manager.update_config({
                "plex_ip_publique": remote_uri or "URL_PLEX", "plex_ip_locale": local_uri or "URL_SECOURS",
                "plex_token": account.authenticationToken, "user_plex": username, "mdp_plex": password
            })
            logging.info("Authentification réussie.")
            return True
        logging.error("Trop de tentatives d'authentification échouées.")
        return False

    def _select_server_and_connections(self, account):
        """Permet à l'utilisateur de choisir un serveur et retourne ses URI."""
        resources = account.resources()
        if not resources:
            return None, None
        resource = resources[0] # Simplifié
        local = next((c.uri for c in resource.connections if c.local), None)
        remote = next((c.uri for c in resource.connections if not c.local), None)
        return local, remote

    def get_library(self, library_name=None):
        """Récupère un objet bibliothèque, en demandant à l'utilisateur si nécessaire."""
        if not self.plex_server: return None
        
        if library_name and library_name != "NOM_BIBLIOTHEQUE":
            try:
                return self.plex_server.library.section(library_name)
            except Exception:
                logging.warning(f"Bibliothèque '{library_name}' introuvable, sélection manuelle...")

        show_sections = [s for s in self.plex_server.library.sections() if s.type == 'show']
        print("\nVeuillez sélectionner une bibliothèque de séries:")
        for i, section in enumerate(show_sections):
            print(f"  {i+1}. {section.title}")
        
        choice = int(input("Votre choix : ")) - 1
        selected_library = show_sections[choice]
        self.db_manager.update_config({"bibliotheque": selected_library.title})
        return selected_library
        
    def unlock_all_fields_in_library(self, library):
        """Déverrouille les champs de métadonnées pour une bibliothèque donnée."""
        logging.info(f"Déverrouillage des champs pour la bibliothèque '{library.title}'...")
        try:
            # Champs pour les séries, saisons et épisodes
            show_fields = ['title', 'summary', 'thumb', 'art', 'banner', 'theme', 'rating', 'originallyAvailableAt', 'contentRating', 'studio', 'genre', 'tag']
            season_fields = ['title', 'summary', 'thumb', 'art']
            episode_fields = ['title', 'summary', 'thumb', 'rating', 'originallyAvailableAt']

            for field in show_fields:
                library.unlockAllField(field, libtype='show')
            for field in season_fields:
                library.unlockAllField(field, libtype='season')
            for field in episode_fields:
                library.unlockAllField(field, libtype='episode')

            logging.info("Déverrouillage des champs terminé.")
        except Exception as e:
            logging.error(f"Erreur lors du déverrouillage des champs : {e}")

class FankaiApiManager:
    """Gère toutes les communications avec l'API Fankai."""
    def __init__(self, config):
        self.base_url = config.api_base_url

    def _get(self, endpoint):
        """Les requêtes GET."""
        try:
            response = requests.get(f"{self.base_url}{endpoint}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logging.error(f"Erreur API pour {endpoint}: {e}")
            return None

    def get_series_list(self):
        return self._get("/series") or []
        
    def get_serie_details(self, serie_id):
        details = self._get(f"/series/{serie_id}")
        if details:
            details['theme_music'] = f"{self.base_url}/series/{serie_id}/theme"
        return details

    def get_serie_actors(self, serie_id):
        data = self._get(f"/series/{serie_id}/actors")
        return data.get('actors') if data and 'actors' in data else data

    def get_serie_seasons(self, serie_id):
        data = self._get(f"/series/{serie_id}/seasons")
        return data.get('seasons') if data else []

    def get_season_episodes(self, season_id):
        data = self._get(f"/seasons/{season_id}/episodes")
        return data.get('episodes') if data else []

class MetadataUpdater:
    """Contient la logique pour appliquer les métadonnées aux objets Plex."""
    
    def update_show(self, show, details, actors):
        """Met à jour les métadonnées d'une série."""
        logging.info(f"Mise à jour des métadonnées pour la série : '{show.title}'")
        
        edits = {
            "title.value": details.get('title'),
            "title.locked": 1,
            "titleSort.value": details.get('title_for_plex'),
            "titleSort.locked": 1,
            "originalTitle.value": details.get('original_title'),
            "originalTitle.locked": 1,
            "summary.value": details.get('plot'),
            "summary.locked": 1,
            "year.value": details.get('year'),
            "year.locked": 1,
            "studio.value": details.get('studio'),
            "studio.locked": 1,
            "originallyAvailableAt.value": details.get('premiered'),
            "originallyAvailableAt.locked": 1,
            "rating.value": details.get('rating_value'),
            "rating.locked": 1,
            "audienceRating.value": details.get('rating_value'),
            "audienceRating.locked": 1,
            "contentRating.value": details.get('mpaa'),
            "contentRating.locked": 1,
            "country.value": details.get('country'),
            "country.locked": 1
        }
        show.edit(**{k: v for k, v in edits.items() if v is not None})
        
        # Gestion des genres
        if details.get('genres'):
            try:
                show.addGenre(details['genres'].split(','), locked=True)
            except Exception as e:
                logging.error(f"Erreur lors de la mise à jour des genres : {e}")

        # Gestion des acteurs
        if actors:
            try:
                # La méthode pour effacer tous les acteurs est d'éditer le champ avec une liste vide
                show.edit(**{'actor[].locked': 0, 'actor[]': []})
                show.reload()

                # Ajouter les nouveaux acteurs
                sorted_actors = sorted(actors, key=lambda x: (0 if x.get('role') == 'Kaïeur' else 1, x.get('id', 0)))
                actor_edits = {}
                for i, actor in enumerate(sorted_actors):
                    actor_edits[f'actor[{i}].tag.tag'] = actor.get('name')
                    actor_edits[f'actor[{i}].role.tag.tag'] = actor.get('role')
                    if actor.get("thumb_url"):
                         actor_edits[f'actor[{i}].tag.thumb'] = actor.get("thumb_url")
                
                actor_edits['actor.locked'] = 1
                show.edit(**actor_edits)
            except Exception as e:
                logging.error(f"Erreur lors de la mise à jour des acteurs : {e}")


        # Gestion des autres métadonnées
        try:
            if details.get('poster_image'):
                show.uploadPoster(url=details['poster_image'])
            if details.get('fanart_image'):
                show.uploadArt(url=details['fanart_image'])
            if details.get('theme_music'):
                show.uploadTheme(url=details['theme_music'])
            # Norlamement Plex doit ajouter la gestion des logos d'ici peu, comme ça ce sera fait
            if details.get('logo_image'):
                show.uploadLogo(url=details['logo_image'])
        except Exception as e:
             logging.error(f"Erreur lors de la mise à jour de : {e}")

    def update_season(self, season, season_data):
        """Met à jour les métadonnées d'une saison."""
        logging.info(f"  Mise à jour saison {season.index}...")
        
        edits = {
            "title.value": season_data.get('title'),
            "title.locked": 1,
            "summary.value": season_data.get('plot'),
            "summary.locked": 1,
            "originallyAvailableAt.value": season_data.get('premiered'),
            "originallyAvailableAt.locked": 1
        }
        season.edit(**{k: v for k, v in edits.items() if v is not None})

        if season_data.get('poster_image'):
            season.uploadPoster(url=season_data['poster_image'])
        if season_data.get('fanart_image'):
            season.uploadArt(url=season_data['fanart_image'])
            
    def update_episode(self, episode, episode_data):
        """Met à jour les métadonnées d'un épisode."""
        edits = {
            "title.value": episode_data.get('title'),
            "title.locked": 1,
            "summary.value": episode_data.get('plot'),
            "summary.locked": 1,
            "originallyAvailableAt.value": episode_data.get('aired'),
            "originallyAvailableAt.locked": 1,
            "contentRating.value": episode_data.get('mpaa'),
            "contentRating.locked": 1,
            "studio.value": episode_data.get('studio'),
            "studio.locked": 1
        }
        episode.edit(**{k: v for k, v in edits.items() if v is not None})
        
        if episode_data.get('thumb_image'):
            episode.uploadPoster(url=episode_data['thumb_image'])

class Application:
    """Classe principale qui orchestre l'application."""
    def __init__(self):
        self.args = self._parse_arguments()
        self.config = Config()
        self.db_manager = DatabaseManager(self.config.db_path)
        self.plex_manager = PlexManager(self.db_manager)
        self.api_manager = FankaiApiManager(self.config)
        self.updater = MetadataUpdater()

    def _parse_arguments(self):
        """Gère les arguments de la ligne de commande."""
        parser = argparse.ArgumentParser(description="Fankai-Metadata")
        parser.add_argument("--series", help="Noms des séries à mettre à jour, séparés par des virgules")
        return parser.parse_args()

    def run(self):
        """Point d'entrée principal de l'application."""
        self.config.ensure_dirs_exist()
        os.chdir(self.config.fankai_app_path)
        setup_logging(self.config.log_path)

        print(pyfiglet.figlet_format("FANKAI-META"))
        logging.info("Mise à jour des métadonnées Plex avec l'API Fan-Kai.")

        if not self.plex_manager.connect():
            logging.error("Impossible de continuer sans connexion à Plex.")
            return

        db_config = self.db_manager.load_config()
        library = self.plex_manager.get_library(db_config.get("bibliotheque"))
        if not library: return

        self.plex_manager.unlock_all_fields_in_library(library)
        all_shows_in_plex = library.all()
        shows_to_update = self._select_shows_to_update(all_shows_in_plex)
        
        logging.info("Scan des fichiers Plex en cours...")
        library.update()
        
        api_series_list = self.api_manager.get_series_list()

        for show in tqdm(shows_to_update, desc="Séries"):
            self._process_show(show, api_series_list)
        
        logging.info("Mise à jour des métadonnées terminée !")

    def _select_shows_to_update(self, all_shows):
        """Sélectionne les séries à mettre à jour en fonction des arguments ou du choix de l'utilisateur."""
        if self.args.series:
            names_to_find = {name.strip().lower() for name in self.args.series.split(',')}
            return [s for s in all_shows if s.title.lower() in names_to_find]

        choice = input("Mettre à jour toutes les séries ou en sélectionner ? (tout/select): ").lower()
        if choice == 'select':
            print("Séries disponibles :")
            for i, show in enumerate(all_shows):
                print(f"  {i+1}. {show.title}")
            
            selected_indices_str = input("Entrez les numéros des séries à mettre à jour (ex: 1, 3, 5): ")
            selected_shows = []
            try:
                indices = [int(idx.strip()) - 1 for idx in selected_indices_str.split(',')]
                for i in indices:
                    if 0 <= i < len(all_shows):
                        selected_shows.append(all_shows[i])
                    else:
                        logging.warning(f"Numéro '{i+1}' invalide, ignoré.")
                return selected_shows
            except ValueError:
                logging.error("Entrée invalide. Aucune série sélectionnée.")
                return []
        return all_shows

    def _process_show(self, show, api_series_list):
        """Traite une série individuelle : trouve les correspondances et met à jour."""
        show_title_lower = show.title.lower()
        
        # D'abord essayer de matcher sur title_for_plex
        api_serie = next((s for s in api_series_list 
                         if s.get('title_for_plex', '').lower() == show_title_lower), None)

        # Si pas de match, essayer sur show_title
        if not api_serie:
            api_serie = next((s for s in api_series_list 
                            if s.get('show_title', '').lower() == show_title_lower), None)

        if not api_serie:
            logging.warning(f"Aucune correspondance API trouvée pour '{show.title}'")
            return
        
        serie_id = api_serie['id']
        details = self.api_manager.get_serie_details(serie_id)
        actors = self.api_manager.get_serie_actors(serie_id)
        api_seasons = self.api_manager.get_serie_seasons(serie_id)
        
        if not details: return
        self.updater.update_show(show, details, actors)

        for season in show.seasons():
            api_season = next((s for s in api_seasons if s.get('season_number') == season.index), None)
            if not api_season: continue
            
            self.updater.update_season(season, api_season)
            api_episodes = self.api_manager.get_season_episodes(api_season['id'])
            
            for episode in tqdm(season.episodes(), desc=f"Saison {season.index}", leave=False):
                api_episode = next((e for e in api_episodes if e.get('episode_number') == episode.index), None)
                if api_episode:
                    self.updater.update_episode(episode, api_episode)

# --- Point d'entrée ---

def handle_interrupt(sig, frame):
    """Gère le signal d'interruption (Ctrl+C)."""
    print("\nSignal d'interruption reçu. Au revoir !")
    sys.exit(0)

def clear_host():
    """Nettoie la console."""
    os.system('cls' if os.name == 'nt' else 'clear')

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        app = Application()
        app.run()
    except Exception as e:
        logging.critical(f"Une erreur fatale est survenue: {e}", exc_info=True)
    
    input("\nAppuyez sur Entrée pour quitter.")

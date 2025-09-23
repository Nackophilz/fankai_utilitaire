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
from tqdm import tqdm

# --- Configuration & Initialisation ---

# Désactiver les avertissements de certificat SSL (si nécessaire)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class Config:
    """Centralise la configuration, les chemins et les paramètres de la plateforme."""
    def __init__(self):
        self.current_platform = platform.system()
        self.api_base_url = "https://metadata.fankai.fr"
        self._configure_paths()

    def _configure_paths(self):
        """Définit les chemins spécifiques au système d'exploitation."""
        if self.current_platform == 'Windows':
            app_data_root = Path(os.getenv('APPDATA', ''))
        else: # Pour Linux et macOS
            app_data_root = Path.home() / ".local" / "share" if self.current_platform == 'Linux' else Path.home() / "Library" / "Application Support"
        
        self.fankai_app_path = app_data_root / 'fankai'
        self.log_path = self.fankai_app_path / 'logs'
        self.db_path = self.fankai_app_path / 'fankai.db'

    def ensure_dirs_exist(self):
        """S'assure que les répertoires nécessaires existent."""
        self.fankai_app_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)

def setup_logging(log_path):
    """Configure le logging pour la console et un fichier de log."""
    logfile = log_path / 'fankai_metadata.log'
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(logging.INFO)

    # Handler pour la console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)
    
    # Handler pour le fichier
    file_handler = logging.FileHandler(logfile, 'w', 'utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

    # Intercepter les exceptions non gérées
    sys.excepthook = lambda exc_type, exc_value, exc_traceback: \
        logging.critical("Exception non interceptée", exc_info=(exc_type, exc_value, exc_traceback))

# --- Gestion des Données et API ---

class DatabaseManager:
    """Gère toutes les opérations sur la base de données SQLite."""
    def __init__(self, db_path):
        self.db_path = db_path

    def load_config(self):
        """Charge la configuration depuis la base de données."""
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

class PlexApiManager:
    """Gère l'authentification et l'interaction avec l'API du serveur Plex."""
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.base_url = None
        self.headers = None
        self.session = requests.Session()

    def connect(self):
        """Tente de se connecter au serveur Plex. Si échec, propose une reconfiguration."""
        while not self._try_connect_with_stored_credentials():
            if input("La connexion a échoué. Voulez-vous reconfigurer les identifiants ? (o/n) ").lower() == 'o':
                if not self._gather_and_save_plex_credentials():
                    return False # Échec de la récupération des nouveaux identifiants
            else:
                return False # L'utilisateur ne veut pas reconfigurer
        return True

    def _try_connect_with_stored_credentials(self):
        """Tente une connexion en utilisant les informations stockées dans la base de données."""
        config = self.db_manager.load_config()
        token = config.get("plex_token")
        urls = [url for url in [config.get("plex_ip_locale"), config.get("plex_ip_publique")] if url and url not in ["URL_PLEX", "URL_SECOURS"]]

        if not token or token == "TOKEN_PLEX" or not urls:
            return False

        self.headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
        self.session.headers.update(self.headers)

        for url in urls:
            try:
                response = self.session.get(f"{url}/", timeout=5, verify=False)
                response.raise_for_status()
                self.base_url = url
                logging.info(f"Connecté avec succès à Plex via {url}")
                return True
            except requests.RequestException:
                logging.warning(f"Échec de la connexion à {url}, essai de l'URL suivante...")
        return False

    def _gather_and_save_plex_credentials(self):
        """
        Utilise la bibliothèque `plexapi` uniquement pour le processus d'authentification initiale,
        qui gère de manière fiable le 2FA et la découverte de serveurs.
        """
        try:
            from plexapi.myplex import MyPlexAccount
            from plexapi.exceptions import TwoFactorRequired
        except ImportError:
            logging.error("La bibliothèque 'plexapi' est requise pour la configuration initiale.")
            logging.error("Veuillez l'installer avec : pip install plexapi")
            return False

        clear_host()
        logging.info("Configuration de la connexion à votre compte Plex.")
        
        username = input("Adresse e-mail Plex : ")
        password = getpass("Mot de passe Plex : ")
        
        try:
            account = MyPlexAccount(username, password)
        except TwoFactorRequired:
            code = input("Code d'authentification à deux facteurs (2FA) : ")
            account = MyPlexAccount(username, password, code=code)
        except Exception as e:
            logging.error(f"Échec de l'authentification Plex : {e}")
            return False
        
        resources = [r for r in account.resources() if r.product == 'Plex Media Server']
        if not resources:
            logging.error("Aucun serveur Plex Media Server trouvé pour ce compte.")
            return False
        
        # Sélection du serveur
        if len(resources) > 1:
            print("Plusieurs serveurs trouvés :")
            for i, res in enumerate(resources):
                print(f"  {i+1}. {res.name} (Propriétaire: {res.ownerId == account.id})")
            choice = int(input("Choisissez un serveur : ")) - 1
            resource = resources[choice]
        else:
            resource = resources[0]

        local_conn = next((c.uri for c in resource.connections if c.local), None)
        remote_conn = next((c.uri for c in resource.connections if not c.local), None)

        self.db_manager.update_config({
            "plex_ip_publique": remote_conn or "URL_PLEX",
            "plex_ip_locale": local_conn or "URL_SECOURS",
            "plex_token": resource.accessToken,
            "user_plex": username,
            "mdp_plex": ""
        })
        logging.info("Authentification et configuration du serveur réussies.")
        return True

    def _request(self, method, endpoint, **kwargs):
        """Méthode générique pour effectuer des requêtes à l'API Plex."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self.session.request(method, url, verify=False, **kwargs)
            response.raise_for_status()
            if response.headers.get('Content-Type', '').startswith('application/json'):
                return response.json().get('MediaContainer', {})
            return True
        except requests.RequestException as e:
            logging.warning(f"Appel API ({method.upper()} {url}) a échoué: {e}")
            if e.response is not None:
                logging.warning(f"  Détail de l'échec: {e.response.text}")
            return None

    def get_library_sections(self):
        return self._request('get', '/library/sections')

    def get_all_items_in_section(self, section_key):
        return self._request('get', f'/library/sections/{section_key}/all')

    def get_metadata_children(self, rating_key):
        return self._request('get', f'/library/metadata/{rating_key}/children')

    def update_metadata(self, rating_key, params):
        """Met à jour les métadonnées pour un item spécifique."""
        endpoint = f'/library/metadata/{rating_key}'
        return self._request('put', endpoint, params=params)

    def rate_item(self, rating_key, rating_value):
        """Met à jour la note personnelle (étoiles) pour un item."""
        try:
            # L'API Plex attend une note sur 10
            rating = float(rating_value)
            endpoint = '/rate'
            params = {'key': rating_key, 'rating': rating, 'identifier': 'com.plexapp.plugins.library'}
            return self._request('put', endpoint, params=params)
        except (ValueError, TypeError):
            logging.warning(f"Valeur de note invalide pour l'item {rating_key}: {rating_value}")
            return None

    def upload_image(self, rating_key, image_type, image_url):
        endpoint = f'/library/metadata/{rating_key}/{image_type}'
        return self._request('post', endpoint, params={'url': image_url})
        
    def refresh_section(self, section_key):
        return self._request('get', f'/library/sections/{section_key}/refresh')


class FankaiApiManager:
    """Gère les communications avec l'API Fankai."""
    def __init__(self, base_url):
        self.base_url = base_url

    def _get(self, endpoint):
        """Effectue une requête GET sur l'API Fankai."""
        try:
            response = requests.get(f"{self.base_url}{endpoint}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logging.error(f"Erreur API Fankai ({endpoint}): {e}")
            return None

    def get_series_list(self): return self._get("/series") or []
    def get_serie_details(self, serie_id): return self._get(f"/series/{serie_id}")
    def get_serie_actors(self, serie_id): return (self._get(f"/series/{serie_id}/actors") or {}).get('actors')
    def get_serie_seasons(self, serie_id): return (self._get(f"/series/{serie_id}/seasons") or {}).get('seasons', [])
    def get_season_episodes(self, season_id): return (self._get(f"/seasons/{season_id}/episodes") or {}).get('episodes', [])

# --- Logique de Mise à Jour ---

class MetadataUpdater:
    """Applique les métadonnées Fankai aux items Plex via l'API."""
    def __init__(self, plex_manager):
        self.plex = plex_manager

    def unlock_all_show_fields(self, show_key):
        """Déverrouille tous les champs de métadonnées pertinents pour une série."""
        logging.info("  > Déverrouillage des champs de la série...")
        unlock_params = {
            "title.locked": 0, "titleSort.locked": 0, "originalTitle.locked": 0,
            "summary.locked": 0, "studio.locked": 0, "originallyAvailableAt.locked": 0,
            "rating.locked": 0, "audienceRating.locked": 0, "contentRating.locked": 0,
            "status.locked": 0, "genre.locked": 0, "actor.locked": 0,
        }
        if self.plex.update_metadata(show_key, unlock_params):
             logging.info("  > Déverrouillage réussi.")
        else:
            logging.warning("  > Échec du déverrouillage des champs.")

    def update_show(self, show, details, actors):
        """Met à jour les métadonnées d'une série."""
        logging.info(f"\n--- Mise à jour de la série : '{show['title']}' ---")
        show_key = show['ratingKey']
        
        # Étape 1: Déverrouiller tous les champs
        self.unlock_all_show_fields(show_key)

        # Étape 2: Construire le dictionnaire des métadonnées textuelles
        params = {
            "title.value": details.get('title'), "title.locked": 1,
            "titleSort.value": details.get('title_for_plex'), "titleSort.locked": 1,
            "originalTitle.value": details.get('original_title'), "originalTitle.locked": 1,
            "summary.value": details.get('plot'), "summary.locked": 1,
            "studio.value": details.get('studio'), "studio.locked": 1,
            "originallyAvailableAt.value": details.get('premiered'), "originallyAvailableAt.locked": 1,
            "rating.value": details.get('rating_value'), "rating.locked": 1,
            "audienceRating.value": details.get('rating_value'), "audienceRating.locked": 1,
            "contentRating.value": details.get('mpaa'), "contentRating.locked": 1,
        }
        logging.info(f"  > Titre: {details.get('title')} | Titre original: {details.get('original_title')}")
        logging.info(f"  > Studio: {details.get('studio')} | Date de sortie: {details.get('premiered')}")
        
        # Gestion du statut (En cours / Terminé)
        fankai_status = details.get('status')
        if fankai_status:
            status_map = {'en cours': 1, 'terminé': 2}
            plex_status = status_map.get(fankai_status.lower())
            if plex_status:
                params["status.value"] = plex_status
                params["status.locked"] = 1
                logging.info(f"  > Statut: {fankai_status.capitalize()}")

        # Gestion des genres (efface les anciens avant d'ajouter les nouveaux)
        params['genre[].tag.tag-'] = 1 
        if details.get('genres'):
            genres_list = [g.strip() for g in details['genres'].split(',')]
            for i, genre in enumerate(genres_list):
                params[f'genre[{i}].tag.tag'] = genre
            params['genre.locked'] = 1
            logging.info(f"  > Genres: {', '.join(genres_list)}")

        # Gestion des acteurs (efface les anciens avant d'ajouter les nouveaux)
        params['actor[].tag.tag-'] = 1
        if actors:
            has_actor_images = any(a.get("thumb_url") for a in actors)
            if has_actor_images:
                logging.info("  > Mise à jour des photos des acteurs...")

            sorted_actors = sorted(actors, key=lambda x: (0 if x.get('role') == 'Kaïeur' else 1, x.get('id', 0)))
            actor_names = [a.get('name') for a in sorted_actors]
            for i, actor in enumerate(sorted_actors):
                params[f'actor[{i}].tag.tag'] = actor.get('name')
                params[f'actor[{i}].role.tag.tag'] = actor.get('role')
                if actor.get("thumb_url"):
                    params[f'actor[{i}].tag.thumb'] = actor.get("thumb_url")
            params['actor.locked'] = 1
            logging.info(f"  > Acteurs: {', '.join(actor_names[:5])}{'...' if len(actor_names) > 5 else ''}")

        # Étape 3: Envoi de la requête de mise à jour
        logging.info("  > Envoi des métadonnées textuelles...")
        if self.plex.update_metadata(show_key, params):
            logging.info("  > Mise à jour des métadonnées textuelles réussie.")
        else:
            logging.error("  > ÉCHEC de la mise à jour des métadonnées textuelles.")

        # Étape 4: Mise à jour de la note
        rating_value = details.get('rating_value')
        if rating_value is not None:
            logging.info(f"  > Application de la note (étoiles): {rating_value}/10")
            if self.plex.rate_item(show_key, rating_value):
                logging.info("  > Mise à jour de la note réussie.")
            else:
                logging.error("  > ÉCHEC de la mise à jour de la note.")

        # Étape 5: Mise à jour des images
        if details.get('poster_image'):
            logging.info("  > Mise à jour du poster...")
            if self.plex.upload_image(show_key, 'posters', details['poster_image']):
                logging.info("  > Mise à jour du poster réussie.")
            else:
                logging.error("  > ÉCHEC de la mise à jour du poster.")

        if details.get('fanart_image'):
            logging.info("  > Mise à jour du fanart (background)...")
            if self.plex.upload_image(show_key, 'arts', details['fanart_image']):
                logging.info("  > Mise à jour du fanart réussie.")
            else:
                logging.error("  > ÉCHEC de la mise à jour du fanart.")


    def update_season(self, season, season_data):
        """Met à jour les métadonnées d'une saison."""
        logging.info(f"    > Saison {season['index']}: '{season_data.get('title')}'")
        params = {
            "title.value": season_data.get('title'), "title.locked": 1,
            "summary.value": season_data.get('plot'), "summary.locked": 1
        }
        self.plex.update_metadata(season['ratingKey'], params)
        if season_data.get('poster_image'): self.plex.upload_image(season['ratingKey'], 'posters', season_data['poster_image'])

    def update_episode(self, episode, episode_data):
        """Met à jour les métadonnées d'un épisode."""
        params = {
            "title.value": episode_data.get('title'), "title.locked": 1,
            "summary.value": episode_data.get('plot'), "summary.locked": 1,
            "originallyAvailableAt.value": episode_data.get('aired'), "originallyAvailableAt.locked": 1
        }
        self.plex.update_metadata(episode['ratingKey'], params)
        if episode_data.get('thumb_image'): self.plex.upload_image(episode['ratingKey'], 'posters', episode_data['thumb_image'])

# --- Application Principale ---

class Application:
    """Orchestre l'exécution du script."""
    def __init__(self):
        self.args = self._parse_arguments()
        self.config = Config()
        self.db_manager = DatabaseManager(self.config.db_path)
        self.plex_manager = PlexApiManager(self.db_manager)
        self.fankai_manager = FankaiApiManager(self.config.api_base_url)
        self.updater = None

    def _parse_arguments(self):
        """Gère les arguments de la ligne de commande."""
        parser = argparse.ArgumentParser(description="Fankai-Metadata: Met à jour les métadonnées Plex avec l'API Fan-Kai.")
        parser.add_argument("--series", help="Noms des séries à mettre à jour, séparés par des virgules.")
        return parser.parse_args()

    def run(self):
        """Point d'entrée principal de l'application."""
        self.config.ensure_dirs_exist()
        os.chdir(self.config.fankai_app_path)
        setup_logging(self.config.log_path)
        
        print(pyfiglet.figlet_format("FANKAI-META"))
        logging.info("Mise à jour des métadonnées Plex avec l'API Fan-Kai (Mode API Directe).")

        if not self.plex_manager.connect():
            logging.error("Impossible de continuer sans connexion à Plex.")
            return

        library = self._select_library()
        if not library: return
        
        self.updater = MetadataUpdater(self.plex_manager)
        
        all_plex_shows = (self.plex_manager.get_all_items_in_section(library['key']) or {}).get('Metadata', [])
        shows_to_update = self._select_shows_to_update(all_plex_shows)
        
        logging.info("Lancement du scan de la bibliothèque Plex en arrière-plan...")
        self.plex_manager.refresh_section(library['key'])
        
        all_fankai_series = self.fankai_manager.get_series_list()
        
        for show in tqdm(shows_to_update, desc="Séries"):
            self._process_show(show, all_fankai_series)
        
        logging.info("\nMise à jour des métadonnées terminée !")

    def _select_library(self):
        """Permet à l'utilisateur de choisir une bibliothèque de séries."""
        db_config = self.db_manager.load_config()
        library_name = db_config.get("bibliotheque")
        
        sections_data = self.plex_manager.get_library_sections()
        if not sections_data or 'Directory' not in sections_data:
            logging.error("Impossible de récupérer les bibliothèques Plex.")
            return None
        
        all_sections = sections_data['Directory']
        show_sections = [s for s in all_sections if s.get('type') == 'show']
        
        if library_name and library_name != "NOM_BIBLIOTHEQUE":
            found = next((s for s in show_sections if s['title'] == library_name), None)
            if found: return found
            logging.warning(f"Bibliothèque pré-configurée '{library_name}' introuvable. Sélection manuelle...")
            
        if not show_sections:
            logging.error("Aucune bibliothèque de type 'Série TV' trouvée sur votre serveur.")
            return None
            
        print("\nVeuillez sélectionner une bibliothèque de séries:")
        for i, section in enumerate(show_sections):
            print(f"  {i+1}. {section['title']}")
        
        try:
            choice = int(input("Votre choix : ")) - 1
            if 0 <= choice < len(show_sections):
                selected_library = show_sections[choice]
                self.db_manager.update_config({"bibliotheque": selected_library['title']})
                return selected_library
        except (ValueError, IndexError):
            logging.error("Sélection invalide.")
            return None

    def _select_shows_to_update(self, all_shows):
        """Sélectionne les séries à traiter en fonction des arguments ou d'un choix utilisateur."""
        if self.args.series:
            names_to_find = {name.strip().lower() for name in self.args.series.split(',')}
            return [s for s in all_shows if s['title'].lower() in names_to_find]

        choice = input("\nMettre à jour toutes les séries de la bibliothèque ou en sélectionner ? (tout/select): ").lower()
        if choice == 'select':
            print("\nSéries disponibles dans la bibliothèque :")
            for i, show in enumerate(all_shows):
                print(f"  {i+1}. {show['title']}")
            
            selected_indices_str = input("Entrez les numéros des séries à traiter (ex: 1, 3, 5) : ")
            try:
                indices = [int(idx.strip()) - 1 for idx in selected_indices_str.split(',')]
                return [all_shows[i] for i in indices if 0 <= i < len(all_shows)]
            except ValueError:
                logging.error("Entrée invalide.")
                return []
        return all_shows

    def _process_show(self, show, all_fankai_series):
        """Traite une série individuelle : trouve la correspondance Fankai et met tout à jour."""
        show_title_lower = show['title'].lower()
        
        # Recherche une correspondance dans les données Fankai
        fankai_serie = next((s for s in all_fankai_series if s.get('title_for_plex', '').lower() == show_title_lower or s.get('show_title', '').lower() == show_title_lower), None)

        if not fankai_serie:
            logging.warning(f"  -> Aucune correspondance Fankai trouvée pour '{show['title']}'. Ignoré.")
            return
        
        serie_id = fankai_serie['id']
        details = self.fankai_manager.get_serie_details(serie_id)
        actors = self.fankai_manager.get_serie_actors(serie_id)
        fankai_seasons = self.fankai_manager.get_serie_seasons(serie_id)
        
        if not details: return
        self.updater.update_show(show, details, actors)

        plex_seasons = (self.plex_manager.get_metadata_children(show['ratingKey']) or {}).get('Metadata', [])
        for season in plex_seasons:
            if season.get('index', -1) == 0: continue # Ignorer la saison "Spéciaux"
            
            fankai_season = next((s for s in fankai_seasons if s.get('season_number') == season['index']), None)
            if not fankai_season: continue
            
            self.updater.update_season(season, fankai_season)
            fankai_episodes = self.fankai_manager.get_season_episodes(fankai_season['id'])
            
            plex_episodes = (self.plex_manager.get_metadata_children(season['ratingKey']) or {}).get('Metadata', [])
            for episode in tqdm(plex_episodes, desc=f"    Épisodes", leave=False, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}'):
                fankai_episode = next((e for e in fankai_episodes if e.get('episode_number') == episode['index']), None)
                if fankai_episode:
                    self.updater.update_episode(episode, fankai_episode)

def clear_host():
    """Nettoie la console."""
    os.system('cls' if platform.system() == 'Windows' else 'clear')

def handle_interrupt(sig, frame):
    """Gère le signal d'interruption (Ctrl+C)."""
    print("\n\nOpération interrompue par l'utilisateur. Au revoir !")
    sys.exit(0)

def main():
    """Point d'entrée principal du script."""
    signal.signal(signal.SIGINT, handle_interrupt)
    app = None
    try:
        app = Application()
        app.run()
    except Exception as e:
        logging.critical(f"Une erreur fatale est survenue: {e}", exc_info=True)
    finally:
        if app and len(sys.argv) == 1:
            input("\nAppuyez sur Entrée pour quitter.")

if __name__ == '__main__':
    main()


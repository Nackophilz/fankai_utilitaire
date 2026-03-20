<div align="center">

![Logo](assets/Logo_Fankai.png)

# Fankai Utilitaire (Fankai-Maj)

_Par des fans, pour des fans_

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![OS Support](https://img.shields.io/badge/OS-Windows%20%7C%20Linux%20%7C%20macOS-success.svg)]()
[![License](https://img.shields.io/github/license/Nackophilz/fankai_utilitaire)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Nackophilz/fankai_utilitaire?style=social)](https://github.com/Nackophilz/fankai_utilitaire/stargazers)

[**Explorer le projet**](https://github.com/Nackophilz/fankai_utilitaire) · [**Signaler un bug**](https://github.com/Nackophilz/fankai_utilitaire/issues/new)

</div>

## 📖 À propos

**Fankai-Maj** est une suite d'outils automatisée conçue par des fans, pour les fans. Notamment ceux souhaitant maintenir leur médiathèque parfaitement organisée. Finies les heures passées à renommer des fichiers, créer des dossiers ou configurer Plex manuellement. L'outil fait tout pour vous, en arrière-plan.

## 🌌 L'Écosystème Fankai

Fankai-Maj est le cœur du système, mais il s'intègre de manière transparente avec vos serveurs multimédias favoris grâce à nos plugins dédiés :

* 🎬 **[Plugin Jellyfin / Emby](https://github.com/Nackophilz/fankai_jellyfin)** : Retrouvez vos métadonnées Fankai directement sur vos serveurs Jellyfin et Emby.
* 🟠 **Plugin Natif Plex** : Un plugin sur-mesure, **100% compatible avec la version 1.43+** et les nouveaux agents Plex, pour une intégration parfaite et une remontée des informations sans friction.

## ✨ Fonctionnalités Principales

Notre suite se divise en plusieurs modules intelligents :

* 🚀 **Fankai (Launcher)** : Interface de démarrage avec mise à jour automatique depuis GitHub. Vous avez toujours la dernière version !
* ⚙️ **Fankai-Config** : Assistant de configuration qui se connecte à votre compte Plex (supporte le 2FA) et crée automatiquement votre bibliothèque dédiée avec les bons paramètres.
* 📦 **Fankai-Placement** : Le moteur de tri.
    * **Fuzzy Matching :** Associe intelligemment vos fichiers vidéo locaux avec les métadonnées NFO officielles de l'API Fankai.
    * **Économie d'espace :** Supporte la création de *Hardlinks* (liens physiques) pour continuer à partager vos fichiers (seed) sans doubler l'espace disque utilisé.
* 🔄 **Fankai-Sync & Service** : Maintient vos "Packs" à jour en synchronisant automatiquement des dossiers ciblés (via GitLab *sparse-checkout*). Configure même les tâches planifiées (Cron, Task Scheduler, Launchd) pour tourner de manière autonome.

## 💻 Compatibilité

Fankai-Maj est fièrement **Cross-Platform**. Le code s'adapte automatiquement à votre environnement :
* **Windows** (Fichiers `.exe`, AppData, Task Scheduler)
* **Linux** (Support natif et ARM, Cron)
* **macOS** (Support natif et ARM, Launchd)

## 🛠️ Installation et Démarrage rapide

Assurez-vous d'avoir Python 3.8+ installé sur votre machine.

1. **Clonez le dépôt :**
   ```bash
   git clone [https://github.com/Nackophilz/fankai_utilitaire.git](https://github.com/Nackophilz/fankai_utilitaire.git)
   cd fankai_utilitaire
   ```

2.  **Installez les dépendances :**

    ```bash
    pip install -r requirements.txt
    ```

3.  **Lancez la configuration initiale si besoin (PLEX):**
    Commencez par relier votre compte Plex et télécharger les outils nécessaires.

    ```bash
    python src/Fankai-Config.py
    ```

4.  **Utilisez l'application au quotidien :**

    ```bash
    python src/Fankai.py
    ```

## 🤝 Contributions

Toute aide est **grandement appréciée** pour faire grandir ce projet communautaire \!

  * Pour des suggestions d'ajouts ou de modifications, n'hésitez pas à [ouvrir une issue](https://www.google.com/url?sa=E&source=gmail&q=https://github.com/Nackophilz/fankai_utilitaire/issues/new) pour en discuter.
  * Veuillez soigner votre orthographe et la grammaire dans vos PR.
  * Créez une Pull Request séparée pour chaque nouvelle suggestion ou fonctionnalité.

### Comment contribuer techniquement :

1.  Forkez le projet.
2.  Créez votre branche de fonctionnalité (`git checkout -b feature/NouvelleFonctionnalité`).
3.  Faites vos commits (`git commit -m 'Ajout de NouvelleFonctionnalité'`).
4.  Poussez sur la branche (`git push origin feature/NouvelleFonctionnalité`).
5.  Ouvrez une Pull Request.

-----

*Si cet outil vous a fait gagner du temps dans la gestion de votre serveur, n'hésitez pas à laisser une ⭐, ça fait toujours très plaisir \!*

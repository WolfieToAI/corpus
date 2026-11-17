# Explication technique (FR)

Version courte en français. Le détail complet, en anglais, est dans le [README](../README.md) et
[ARCHITECTURE.md](ARCHITECTURE.md).

## En bref

- **Quoi** : une plateforme RAG (Retrieval-Augmented Generation) multi-tenant pour « discuter avec
  ses documents ».
- **Pour qui** : une entreprise qui veut un assistant (support, juridique, RH) qui répond à partir
  de **ses** documents.
- **Résultat** : une réponse en streaming, où chaque affirmation cite le document et la page
  d'où elle vient.
- **Stack** : Python 3.12, FastAPI, PostgreSQL (pgvector, tsvector, RLS), SQLAlchemy async,
  Alembic, Docker.

## Architecture

- **Deux processus** : l'API (FastAPI) reçoit les requêtes ; les workers indexent les documents
  en arrière-plan.
- **Une seule base** : PostgreSQL stocke les documents, les vecteurs, l'index plein texte et la
  file de tâches.
- **Couches** : `api -> services -> core`. Le `core` ne connaît ni FastAPI ni SQL, donc il se
  teste isolément.
- **Fournisseurs interchangeables** : les embeddings et le LLM sont derrière des `Protocol`. On
  change de fournisseur par configuration, et les tests utilisent des faux déterministes.

## Parcours d'un document (ingestion)

- **Upload** : le document, son contenu et sa tâche d'indexation sont enregistrés dans **une
  seule transaction**, donc rien ne peut rester à moitié créé.
- **Réveil** : un trigger `pg_notify` réveille un worker instantanément (`LISTEN/NOTIFY`).
- **Parsing** : PDF, DOCX, HTML, Markdown, texte. Les PDF scannés passent par l'**OCR**
  (Tesseract).
- **Chunking** : découpage en morceaux d'environ 400 tokens, avec un chevauchement de 60 tokens,
  en respectant la structure et en gardant les numéros de page.
- **Embeddings** : chaque chunk devient un vecteur, avec un modèle multilingue local (ONNX) ou
  Voyage AI.
- **Écriture** : les anciens chunks sont remplacés en une transaction, donc réindexer ne crée
  jamais de doublons.

## Parcours d'une question

- **Reformulation** : une question de suivi (« Et pour le R7 ? ») est réécrite en question
  autonome grâce à l'historique.
- **Recherche vectorielle** : on trouve les passages proches **par le sens** (pgvector, index
  HNSW, distance cosinus).
- **Recherche plein texte** : on trouve les passages contenant **les mots exacts** (tsvector,
  index GIN), ce qui est utile pour les codes produits et les noms.
- **Fusion RRF** : les deux classements sont fusionnés d'après les rangs, sans comparer des scores
  incompatibles.
- **Reranking** : un cross-encoder relit les 20 meilleurs passages et les reclasse (optionnel).
- **Génération** : les sources numérotées sont envoyées au LLM, qui doit citer `[n]` ou dire
  « absent des documents ».
- **Vérification** : le serveur ne garde que les citations qui correspondent à des sources
  réellement fournies.
- **Streaming** : les sources, puis le texte mot par mot, puis les citations, envoyés en SSE
  (Server-Sent Events).

## Multi-tenant et sécurité

- **RLS (Row-Level Security)** : chaque transaction tourne avec un rôle restreint et l'identifiant
  du client (`SET LOCAL ROLE` et `set_config`). Une requête qui oublie son `WHERE` ne peut pas lire
  les données d'un autre client.
- **Clés étrangères composites** : `(id, tenant_id)` rend impossible tout lien entre deux
  clients, même pour du code privilégié.
- **Clés API** : seul le hash SHA-256 est stocké, la comparaison se fait en temps constant, et une
  clé est affichée une seule fois puis révocable.
- **404 plutôt que 403** : la ressource d'un autre client « n'existe pas », ce qui empêche de
  deviner les identifiants.
- **Rate limiting** : une limite par client et par minute, en une seule requête SQL atomique,
  correcte avec plusieurs API en parallèle, sans Redis.
- **Injection de prompt** : le texte des documents est échappé et traité comme une donnée, jamais
  comme une instruction.

## File de tâches

- **Pourquoi PostgreSQL et pas Redis** : la tâche est créée dans la même transaction que le
  document, ce qui évite les orphelins comme les tâches fantômes.
- **`FOR UPDATE SKIP LOCKED`** : plusieurs workers prennent des tâches en parallèle, sans jamais
  prendre la même.
- **Bail (lease) et heartbeat** : si un worker plante, sa tâche redevient disponible ; une tâche
  longue prolonge son bail.
- **Fencing** : un worker « zombie » qui se réveille ne peut plus écrire, car chaque écriture
  vérifie le propriétaire.
- **Retries** : backoff exponentiel avec jitter. Les erreurs définitives partent en dead-letter.
- **Idempotence** : un index unique partiel empêche les tâches actives en double.

## Qualité mesurée

- **`corpus-eval`** : un jeu de 40 questions étiquetées (paraphrases, codes exacts, questions
  sans réponse, questions de suivi).
- **Métriques** : recall@k, MRR, nDCG, plus un LLM juge pour la fidélité des réponses.
- **Résultats (Recall@1)** : plein texte 0,74, vectoriel 0,55, hybride 0,84, hybride + reranking
  **0,90**.
- **À retenir** : chaque méthode seule a des angles morts, et leur combinaison les compense.

## Production

- **Observabilité** : métriques Prometheus, dashboard Grafana, alertes, traces OpenTelemetry qui
  suivent un document de l'upload jusqu'au worker, et logs JSON corrélés.
- **Tests de charge** : k6 en CI. Au-delà de la capacité, l'API refuse proprement (`503` avec
  `Retry-After`) au lieu de s'effondrer.
- **Bug trouvé grâce aux tests de charge** : un interblocage du pool de connexions, corrigé et
  couvert par un test de non-régression.
- **Qualité du code** : `mypy --strict`, ruff, 190 tests (isolation, concurrence, reprise après
  crash) sur un vrai PostgreSQL, et des migrations testées dans les deux sens.
- **Déploiement** : Docker Compose en local, Render et Neon pour la [démo en
  ligne](https://corpus-8jf9.onrender.com/demo).

## Choix assumés

- **PostgreSQL plutôt qu'une base vectorielle dédiée** : une seule base à sauvegarder, des
  suppressions en cascade, et le RLS qui protège aussi les vecteurs. Suffisant jusqu'à des
  millions de chunks.
- **Fichiers dans PostgreSQL plutôt que S3** : plus simple à cette échelle. Un `BlobStore` S3
  serait l'étape suivante.
- **Jeu d'évaluation petit, étiqueté à la main** : l'agrandir est la priorité suivante.

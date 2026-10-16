# generic-openapi-mcp

Serveur MCP Python en STDIO qui transforme les opérations décrites par une spécification Swagger 2.0 ou OpenAPI 3.x en tools MCP. Une API s’ajoute par configuration; le moteur n’embarque pas de logique propre à Strava, YouTrack ou une autre API.

Le dépôt de référence [strava-openapi-mcp](https://github.com/Arbodgad/strava-openapi-mcp) sépare déjà parsing, opération normalisée, génération des tools, exécution HTTP et transport MCP. Ce projet reprend ces responsabilités et retire les réglages Strava, les exceptions de son Swagger et le flux OAuth interactif du noyau. Les changements sont implémentés dans un nouvel espace de travail vide; le clone Git de référence n’était pas accessible depuis l’environnement.

## Installation

Python 3.12+ et `uv` sont requis. Depuis le dépôt :

```sh
uv sync --group dev
uv run openapi-mcp --config openapi-mcp.yaml validate
uv run openapi-mcp --config openapi-mcp.yaml list-tools
uv run openapi-mcp --config openapi-mcp.yaml
```

Une fois le dépôt publié, l’entrée `openapi-mcp` peut être lancée avec `uvx --from git+https://github.com/Arbodgad/strava-openapi-mcp openapi-mcp --config /chemin/openapi-mcp.yaml`.

Les diagnostics de CLI vont sur stderr. En mode STDIO, stdout reste réservé au protocole MCP.

## Configuration

Copiez [`openapi-mcp.example.yaml`](openapi-mcp.example.yaml) et indiquez un document JSON/YAML local ou une URL HTTP(S). Un chemin relatif est résolu par rapport au fichier de configuration.

```yaml
apis:
  youtrack:
    spec: ./specs/youtrack-openapi.json
    base_url: http://tcs.mvg.local:8080/api
    allow_hosts: [tcs.mvg.local]
    auth:
      type: bearer
      token_env: YOUTRACK_TOKEN
    tools:
      include_methods: [GET]
      exclude_paths: []
```

Les secrets ne vont pas dans le YAML. Définissez `YOUTRACK_TOKEN` dans l’environnement qui lance le serveur. `auth` peut être un profil par défaut comme ci-dessus ou une table indexée par le nom d’un `securityScheme` OpenAPI pour configurer des credentials distincts.

`base_url` remplace le serveur déclaré dans le document. Sinon, le serveur OpenAPI racine ou les champs Swagger `schemes`, `host` et `basePath` sont utilisés. Les variables de serveur OpenAPI utilisent les valeurs de `server_variables`, puis leurs valeurs par défaut.

Plusieurs API peuvent être déclarées sous `apis`. Les tools sont préfixés par leur identifiant (`youtrack_get_issues_by_issue_id`).

## Sécurité et authentification

- Seules les opérations `GET` sont exposées par défaut. `POST`, `PUT` et `PATCH` demandent à la fois leur présence dans `include_methods` et `allow_write: true`. `DELETE` demande `allow_delete: true`.
- `include_paths` et `exclude_paths` filtrent les chemins par préfixe. Les contrôles s’appliquent à la liste affichée et aux appels directs.
- Les schémas OpenAPI Bearer, HTTP Basic et API key (header, query ou cookie) sont pris en charge. Les credentials sont injectés depuis des variables d’environnement, jamais depuis les arguments du tool.
- Les destinations HTTP(S) locales et privées sont refusées par défaut. Pour une API auto-hébergée, ajoutez son nom d’hôte exact à `allow_hosts`. Les redirections ne sont pas suivies.
- Les documents de spécification sont limités à 5 MiB. Les réponses sont limitées par `max_response_bytes` (2 MiB par défaut, maximum configurable 50 MiB). Les timeouts sont configurables (30 secondes par défaut).
- Les écritures ne sont pas rejouées automatiquement. Les journaux ne contiennent pas de corps de réponse ni de credentials.

Exemple OpenCode (adaptez le chemin du fichier et l’environnement du processus) :

```json
{
  "mcp": {
    "generic-openapi": {
      "type": "local",
      "command": ["uvx", "--from", "git+https://github.com/Arbodgad/strava-openapi-mcp", "openapi-mcp", "--config", "/path/openapi-mcp.yaml"],
      "enabled": true
    }
  }
}
```

## Commandes

- `openapi-mcp --config FILE validate` charge les spécifications, vérifie les versions et les URL de base, puis résume le nombre d’opérations.
- `openapi-mcp --config FILE list-tools [--schemas]` affiche les tools sélectionnés et leurs schémas d’entrée.
- `openapi-mcp --config FILE` démarre le serveur MCP STDIO.

## Compatibilité et limites connues

- Le chargeur accepte Swagger 2.0, OpenAPI 3.0.x et 3.1.x. Les références locales JSON Pointer sont résolues; les cycles de schéma deviennent des objets ouverts pour éviter une récursion infinie. Les `$ref` externes sont refusés explicitement.
- Les paramètres path, query, header et cookie, les paramètres hérités, les champs `required`, les corps JSON, urlencoded, multipart textuel et `text/plain` sont générés. La sérialisation des tableaux couvre les styles usuels et les `collectionFormat` Swagger courants. Les cas imbriqués complexes `deepObject`, `matrix`, `label`, les téléchargements binaires et les uploads de fichiers ne sont pas pris en charge.
- Les compositions `allOf`, `oneOf`, `anyOf`, `nullable` et les types unions sont conservés sous forme JSON Schema quand les clients MCP l’acceptent; les constructions complexes ne sont pas validées exactement comme un validateur OpenAPI complet.
- Bearer statique, API key et Basic sont implémentés. Le renouvellement OAuth et le flux interactif présent dans le projet Strava de référence ne sont pas migrés; `oauth2` demande un bearer statique configuré.
- Les serveurs déclarés à un niveau opération/path sont respectés sauf si `base_url` surcharge explicitement la spécification. La pagination automatique n’est pas implémentée.
- Le chargement des documents YAML utilise `yaml.safe_load`; aucune extension HTML Swagger UI ni résolution externe automatique n’est faite.
- Le serveur MCP STDIO doit encore être validé contre le SDK MCP réel dans un environnement disposant de `mcp` et de `uv`. L’environnement de travail actuel n’a pas `uv` ni le SDK `mcp`; son sandbox Windows bloque aussi la socket locale qu’asyncio crée, donc le test HTTP simulé est présent mais marqué ignoré uniquement dans ce sandbox.

## Tests

```sh
uv run pytest
uv run ruff check .
```

La suite utilise des documents et transports simulés et ne contacte pas les serveurs réels. Dans un environnement Python local standard, le test de transport HTTP simulé vérifie URL, query, authentification Bearer et corps JSON.

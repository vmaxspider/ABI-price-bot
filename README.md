# ABI Price Bot

Surveille les prix du marché sur abi-tracker.azurewebsites.net et envoie une
alerte Discord quand un item bouge de plus de 15% (modifiable dans `bot.py`,
variable `THRESHOLD_PCT`).

## Installation (GitHub Actions — gratuit, tourne tout seul)

1. Crée un nouveau repo GitHub (public ou privé, peu importe).
2. Mets-y tous les fichiers de ce dossier (bot.py, requirements.txt,
   .github/workflows/bot.yml).
3. Dans le repo GitHub : Settings → Secrets and variables → Actions →
   New repository secret.
   - Nom : `DISCORD_WEBHOOK_URL`
   - Valeur : ton URL de webhook Discord
4. Va dans l'onglet "Actions" du repo, active les workflows si demandé.
5. Le bot tourne automatiquement toutes les 30 minutes. Tu peux aussi le
   lancer manuellement via Actions → ABI Price Bot → Run workflow.

## Test en local (optionnel, avant de mettre sur GitHub)

```bash
pip install -r requirements.txt
playwright install chromium

# Sur Mac/Linux :
export DISCORD_WEBHOOK_URL="ton_url_ici"
# Sur Windows PowerShell :
$env:DISCORD_WEBHOOK_URL="ton_url_ici"

python bot.py
```

Le premier lancement ne déclenchera jamais d'alerte (rien à comparer).
À partir du 2e lancement, les comparaisons commencent.

## Réglages

- `THRESHOLD_PCT` dans `bot.py` : seuil de variation en % pour déclencher
  une alerte (15 par défaut).
- `MINOR_IDS` dans `bot.py` : liste des catégories suivies. Retire-en si tu
  veux te concentrer sur certaines catégories seulement.

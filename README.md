# ResearchAssistant

A desktop app for managing an academic literature workflow on top of
[Zotero](https://www.zotero.org/) and Google's Gemini API. It pulls papers
from a Zotero library into a local Kanban board, lets you triage them by
priority (Trash / Inbox / Interesting / Relevant / Crucial), push changes
back to Zotero as sub-collections, and run cached LLM queries against the
attached PDFs in the context of your thesis goals.

Built with [Flet](https://flet.dev/) (Flutter for Python) and SQLite.

## Features

- **Zotero sync** — pull items from a group or user library, including
  resolving local PDF attachment paths.
- **Kanban triage** — drag papers across five priority columns; ordering is
  persisted locally and can be pushed back to Zotero sub-collections.
- **Gemini-powered analysis** — ask questions against any paper's full text;
  responses are cached by `(paper, prompt)` hash so the same query never hits
  the API twice.
- **Thesis-goal context** — a persistent "core thesis goals" field is
  prepended to every prompt so answers stay aligned with your research focus.
- **Comments & notes** — per-paper notes and freeform comments stored locally.
- **Offline-friendly** — all metadata, notes, and AI responses live in a
  single SQLite file (`research_assistant.db`).

## Requirements

- Python 3.11+
- A Zotero account with an API key and a library (group or personal).
- A Google [Gemini API key](https://aistudio.google.com/apikey).
- For PDF analysis: Zotero items must have local PDF attachments (linked or
  imported) whose paths the Zotero API exposes.

## Setup

```bash
git clone <this-repo>
cd ResearchAssistant

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and fill in your real keys
```

### Zotero sub-collections

For the Kanban / push features to work, create these five **sub-collections**
in your Zotero library (names are case-sensitive):

- `Trash`
- `Inbox`
- `Interesting`
- `Relevant`
- `Crucial`

Each sub-collection maps to a Kanban column. Papers not in any of these go to
`Inbox` on first sync.

## Running

```bash
python main.py
```

The first launch creates `research_assistant.db` in the project root. Click
**Sync Zotero** in the sidebar to pull your library.

## Project layout

```
api/            Gemini and Zotero clients
db/             SQLite schema + helpers
ui/             Flet UI (sidebar, kanban board, paper analyzer)
utils/          PDF text extraction, prompt-hash cache helper
main.py         App entry point
```

## Environment variables

| Variable               | Purpose                                                              |
|------------------------|----------------------------------------------------------------------|
| `gemini_api_key`       | Google Gemini API key (also accepts `GEMINI_API_KEY`).               |
| `ZOTERO_API_KEY`       | Zotero API key.                                                      |
| `ZOTERO_LIBRARY_ID`    | Numeric ID of the Zotero library to sync.                            |
| `ZOTERO_LIBRARY_TYPE`  | `user` or `group`.                                                   |

## License

MIT — see [LICENSE](./LICENSE).

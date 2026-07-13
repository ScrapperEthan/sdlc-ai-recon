# Vendored front-end assets (air-gapped, no CDN)

Drop **`mermaid.min.js`** in this folder to enable live rendering of ` ```mermaid ` diagrams in the
Q&A app answers (e.g. the `flowchart TD` the assistant emits).

## How to add it (one time, on an internet-connected machine)
Download the **minified UMD build** of Mermaid and save it here, keeping the name `mermaid.min.js`.
It exposes a global `window.mermaid` (which `index.html` calls). A current release works
(`mermaid@10` or `@11`). No build step, no npm at runtime — the file is served locally by
`webapp/server.py` at `/static/vendor/mermaid.min.js`.

## Behaviour without it
If this file is absent, the request 404s, `window.mermaid` stays undefined, and mermaid code blocks
simply render as their **source text** (no error, no worse than before). Add the file and the same
answers render as diagrams — no code change needed, just `git pull` + drop the file.

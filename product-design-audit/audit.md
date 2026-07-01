# SDLC AI Code Assistant - Desktop UI Audit

Date: 2026-07-01
Scope: desktop web UI at `http://127.0.0.1:8876/`, running in `LLM_MOCK=1`.
Destination: local folder `product-design-audit/`.

## Captured Steps

1. Start state
   - Screenshot: `screenshots/01-desktop-start.png`
   - Health: usable but visually under-designed.
   - Notes: The page gives a basic prompt example, but the large empty canvas makes the assistant feel unfinished. The top bar communicates "read-only" and citation behavior, which is good, but it is too small to carry trust for an internal engineering tool.

2. Question composed
   - Screenshot: `screenshots/02-desktop-question-composed.png`
   - Health: functional but not reassuring.
   - Notes: The input and send button are clear enough, and keyboard submission is discoverable through placeholder text. The UI does not explain which retrieval tools may run, whether the answer is scoped to mirrored repos, or what evidence quality to expect.

3. Answer with tool trace
   - Screenshots: `screenshots/03-desktop-thinking-or-result.png`, `screenshots/04-desktop-answer-with-tools.png`
   - Health: functional but weak for engineering review.
   - Notes: User and assistant messages are readable, but the answer area does not feel like an engineering evidence surface. Tool trace is only a small text line, so the most important trust signal is visually buried.

## Strengths

- The product surface is simple and low-friction: ask a question, get an answer.
- The app already exposes a useful product promise: read-only behavior and `repo/path:line` citation expectations.
- The fixed-width message column keeps long code answers from becoming unreadable on desktop.
- The footer input stays available, which is right for a chat-style assistant.

## UX Risks

- The first screen does not look like a finished internal product. Most of the viewport is empty dark space, with one small hint card floating near the top.
- Information architecture is too flat. Header, examples, chat, tools, and retrieval scope all share the same visual weight.
- The empty state teaches by paragraph, not by action. Developers would benefit from clickable prompt starters such as "Trace message flow", "Find consumers", "Estimate blast radius", and "Search code".
- Tool execution is under-explained. The current `tools: hubs` chip does not show what was searched, why it was used, or whether the answer is complete.
- The visual language is generic dark chat UI. It does not yet express "cross-repo code intelligence", "evidence", or "safe read-only assistant".

## Accessibility Risks

- The textarea has only placeholder text and no visible label. Placeholder text is not a durable label for assistive technology or for users after typing begins.
- Focus state is visible, but the input area relies heavily on low-contrast dark surfaces and subtle borders.
- The tool trace is small and visually low priority; users with low vision may miss it.
- The header status text is small and muted. Important safety information should be easier to perceive.
- Screenshot evidence cannot prove full keyboard order, screen reader output, or contrast ratios. Those need separate testing.

## Recommended Desktop Redesign

1. Reframe the app as an engineering console, not a plain chatbot.
   - Use a three-zone layout: left context rail, central conversation, right or inline evidence panel.
   - Left rail content: "Read-only", "390 repos", "Sources: mirror / index / CodeGraph", plus tool families.

2. Replace the empty hint card with a stronger start state.
   - Add four prompt cards:
     - "Trace a message route"
     - "Find producers or consumers"
     - "Check impact of a repo change"
     - "Search code with citations"
   - Each card should insert a realistic prompt into the composer.

3. Make answers feel like reviewed engineering output.
   - Add sections inside assistant responses: "Short answer", "Evidence", "Tool steps", "Partial or unverified".
   - Render citations and paths as code-like pills, not plain paragraph text.

4. Promote the tool trace.
   - Turn `tools: hubs` into an expandable run log:
     - `hubs(top=5)`
     - status: completed
     - result count or short summary
   - This builds trust without forcing users to read raw JSON.

5. Improve visual polish without making it flashy.
   - Keep dark mode if preferred, but reduce the single-tone black-gray feel.
   - Use a slightly warmer page background, clearer panel hierarchy, and restrained accent color.
   - Add subtle section dividers, denser spacing in evidence panels, and an explicit app mark.

6. Add missing form semantics.
   - Add a visible or visually hidden label for the question textarea.
   - Add `aria-live` for assistant status and answer arrival.
   - Use `aria-busy` or similar state while a request is running.

## Frontend Code Pointers

- `webapp/static/index.html` currently owns the whole UI.
- CSS tokens are minimal and all dark colors live near the top of the file.
- The tool trace is rendered in the browser after response handling, so it is the best low-risk place to improve trust quickly.
- The empty state and prompt starters can be added without backend changes.

## Backend Notes

- No backend change is required just to make the UI prettier.
- For a better product experience, consider streaming the answer instead of waiting for one blocking `/api/chat` response.
- Consider returning structured fields such as `answer`, `citations`, `tool_steps`, `warnings`, and `partial` so the frontend can render an evidence-first answer instead of one text blob.
- The current mock flow is useful for UI testing. Keep it, but make the mock answer more representative of a real cited engineering answer.

## Evidence Limits

- These findings are based on desktop screenshots and DOM-level interaction, not a full accessibility audit.
- Chrome was used for final screenshots because the in-app browser returned a repeated/tiled screenshot. One browser extension icon appears inside the input in captured images; it is not part of the product UI and should be ignored.
- Mock mode validates the UI flow and tool trace surface, but not real model latency, real citation density, or long-answer layout.

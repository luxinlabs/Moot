# Third-party instructions and framework

| Component | Source and pin | License | How Moot uses it |
|---|---|---|---|
| Pitch Deck Advisor | [amaklakov-droid/pitch-deck-skill](https://github.com/amaklakov-droid/pitch-deck-skill/tree/3397a18aaf3a6ccd28eccd7e74b015860e3dae24) | MIT, © 2026 Alexey Maklakov | Selected narrative/review sections injected into planning, slide writing and revision prompts |
| deckgen | [kingwatam/deckgen](https://github.com/kingwatam/deckgen/tree/41732bae7eecdec5341cafa659a5025f41ac0a3c) | MIT, © 2026 King Wa Tam | Selected source-document, speaker-note and design anti-pattern instructions |
| mcp-use | [Manufact Python SDK](https://pypi.org/project/mcp-use/1.7.1/) | MIT | MCP server, client, optional LLM agent, local Inspector |

Full skill license notices are included alongside each upstream SKILL.md in
`data/skills/vendor/`. Those files are unmodified snapshots. The manifest pins their hashes.
Moot's adapter selects named sections and adds explicit precedence rules; it does not claim the
upstream projects endorse Moot. Upstream code, example presentations and referenced media are
not bundled or executed. Skill guidance is not independently verified fundraising evidence.

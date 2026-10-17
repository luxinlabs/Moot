---
title: Visual style system
summary: Theme, backgrounds, motifs, layouts and drawn visuals. A deck is designed, not just written.
---
Theme = background, text, accent, accent_2, muted (hex) + heading/body font + background_style + motif.
- Body text must contrast with the background at least 4.5:1 (WCAG AA). Accent marks emphasis, title bars and big numbers; accent_2 is a second, complementary highlight used sparingly.
- Pick the look from the product and audience: dark + one vivid accent for developer/infra; light + calm accent for health/finance; warm neutrals for consumer; a serif heading (Georgia, Playfair Display) for premium or editorial tone.
- Fonts: Inter, Helvetica, Georgia, Montserrat, Source Sans Pro, Playfair Display. One heading font + one body font.
Backgrounds (background_style): solid · gradient (subtle diagonal blend toward the accent) · glow (soft accent light in two corners; great for dark stage decks) · grid (faint technical grid; engineering products) · dots (light texture) · bands (diagonal accent stripes; bold/energetic).
Motif (decoration on every slide): none · circles (soft concentric circles, bottom-right) · corner (accent triangles, top-right) · lines (three accent strokes, bottom-left). Keep it consistent across the deck.
Layouts: title · statement (one bold sentence) · bullets · big_number (stat_value + stat_label) · two_column (bullets + bullets_right) · quote · closing.
Drawn visuals (visual + visual_items), rendered as real shapes under the slide's text:
- flow: 3–5 short boxes joined by arrows, for a process or pipeline ("Post job", "Audition", "Hire").
- steps: 3–4 numbered circles with labels, for "how it works" or a failure story.
- timeline: 3–5 milestones on a line, for history, a demo sequence or a roadmap.
- stat_cards: 2–4 cards, each item written "value|label" ("9|MCP tools"); only numbers from the sources.
Show, don't tell: any slide explaining a process, sequence or several proof points should get a visual. Aim for 3+ visual slides, keep items to 2–4 words, and with a visual use at most 3 bullets. Vary layouts; never the same layout three times in a row.

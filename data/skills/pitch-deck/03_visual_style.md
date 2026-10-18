---
title: Visual style system
summary: Theme, backgrounds, motifs, layouts and drawn visuals. A deck is designed, not just written.
provenance:
  repository: https://github.com/alfonsograziano/pptx-gen
  commit: main
  license: MIT
---

# Visual Style System for Pitch Decks

A deck is designed, not just written. The visual system — colors, fonts, backgrounds, motifs, and layouts — must be consistent and purposeful. Every design choice should reinforce the narrative.

## Theme Components

A theme consists of:

### Colors (hex codes)

- **background**: Primary slide background color
- **text**: Primary body text color
- **accent**: Primary emphasis color (titles, big numbers, key highlights)
- **accent_2**: Secondary highlight (used sparingly for contrast)
- **muted**: Secondary text, labels, metadata

### Fonts

- **heading_font**: Font for slide titles and big numbers
- **body_font**: Font for body text, bullets, labels

### Background Style

How the background is rendered:

- **solid**: Flat color
- **gradient**: Subtle diagonal blend toward the accent
- **glow**: Soft accent light in two corners (great for dark stage decks)
- **grid**: Faint technical grid (engineering products)
- **dots**: Light texture pattern
- **bands**: Diagonal accent stripes (bold/energetic)

### Motif

Decoration on every slide:

- **none**: No decoration
- **circles**: Soft concentric circles, bottom-right
- **corner**: Accent triangles, top-right
- **lines**: Three accent strokes, bottom-left

Keep the motif consistent across the deck.

## Color and Contrast Rules

### WCAG AA Compliance

- Body text must contrast with the background at least 4.5:1
- Large text (18pt+) must contrast at least 3:1
- Use online contrast checkers to verify

### Color Psychology

Pick the look from the product and audience:

**Developer / Infrastructure Products**

- Dark background + one vivid accent
- Monospace touches in console/log motifs
- Examples: terminal greens, neon blues, matrix aesthetics

**Health / Finance**

- Light background + calm accent
- Trustworthy colors (blues, teals, greens)
- Clean, professional appearance

**Consumer**

- Warm neutrals (creams, soft grays)
- Friendly accents (corals, warm yellows)
- Approachable, human feel

**Premium / Editorial**

- Serif heading font (Georgia, Playfair Display)
- Sophisticated color palettes
- Magazine-like typography

### Accent Usage

- **accent** marks emphasis, title bars, and big numbers
- **accent_2** is a second, complementary highlight used sparingly
- Don't use more than 2 accent colors per deck
- Reserve accent for the most important information

## Font Selection

### Recommended Fonts

- **Sans-serif**: Inter, Helvetica, Montserrat, Source Sans Pro
- **Serif**: Georgia, Playfair Display
- **Monospace**: Fira Code, JetBrains Mono (for code/technical content)

### Font Pairings

Use one heading font + one body font. Common pairings:

- Inter + Inter (clean, modern)
- Montserrat + Source Sans Pro (geometric, friendly)
- Georgia + Inter (premium, editorial)
- Playfair Display + Montserrat (elegant, distinctive)

### Font Size Hierarchy

- **Slide titles**: 24–32pt (heading font)
- **Body text**: 14–16pt (body font)
- **Big numbers**: 48–72pt (heading font)
- **Labels**: 12–14pt (body font)
- **Speaker notes**: 10–12pt (body font)

## Layouts

Available slide layouts:

1. **title** — Title slide with company name and tagline
2. **statement** — One bold sentence as the main message
3. **bullets** — Standard bullet list
4. **big_number** — Large number + label (stat_value + stat_label)
5. **two_column** — Bullets + bullets_right (side-by-side comparison)
6. **quote** — Large quotation
7. **closing** — Final slide with call to action

Never use the same layout three times in a row. Vary layouts to maintain visual interest.

## Drawn Visuals

Visuals are rendered as real shapes under the slide's text, not images. Use visuals to explain processes, sequences, and proof points.

### Visual Types

**flow**: 3–5 short boxes joined by arrows

- Use for: processes, pipelines, workflows
- Example: "Post job → Audition → Hire → Onboard"

**steps**: 3–4 numbered circles with labels

- Use for: "how it works", failure stories, sequential steps
- Example: "1. Upload → 2. Run → 3. Review"

**timeline**: 3–5 milestones on a line

- Use for: history, demo sequences, roadmaps
- Example: "Q1 → Q2 → Q3 → Q4"

**stat_cards**: 2–4 cards with "value|label" format

- Use for: metrics, KPIs, proof points
- Example: "9|MCP tools", "60%|Bad hire reduction"
- Only numbers from the sources

### Visual Guidelines

- **Show, don't tell**: Any slide explaining a process, sequence, or several proof points should get a visual
- **Aim for 3+ visual slides** in a 12-slide deck
- **Keep items to 2–4 words** in visual elements
- **With a visual, use at most 3 bullets** (the visual carries the message)
- **Vary layouts**: Never the same layout three times in a row

## Background Style Selection

### When to use each style

**solid**

- Clean, professional decks
- Financial services, enterprise software
- When you want maximum readability

**gradient**

- Modern tech products
- Consumer apps
- When you want subtle depth without distraction

**glow**

- Dark stage presentations
- Developer tools, infrastructure
- When you want dramatic effect

**grid**

- Engineering products, data platforms
- Technical audiences
- When you want to emphasize precision

**dots**

- Light texture for visual interest
- When solid is too plain but gradient is too strong
- Subtle background without distraction

**bands**

- Bold, energetic decks
- Consumer-facing products
- When you want dynamic movement

## Motif Selection

### When to use each motif

**none**

- Minimalist decks
- When the content is complex enough
- Professional, conservative audiences

**circles**

- Soft, approachable decks
- Consumer products
- When you want organic feel

**corner**

- Structured, directional decks
- When you want to guide the eye
- Technical products

**lines**

- Structured, process-oriented decks
- When you want to emphasize progression
- Workflow products

## Design Anti-Patterns

### Color

- More than 2 accent colors
- Light text on dark backgrounds (hard to read on projectors)
- Insufficient contrast (fails WCAG AA)
- Random colors without system

### Typography

- More than 2 fonts per deck
- All-caps body text (hard to read)
- Underlining (use bold or color instead)
- Font sizes too small (<14pt for body)

### Layout

- Same layout three times in a row
- Overcrowded slides (no white space)
- Text-only slides with no visuals
- Visuals that don't match the content

### Visuals

- Visuals without purpose (decorative only)
- Overly complex visuals (confusing)
- Visuals that don't match the narrative
- Stock photos instead of drawn shapes

## Design Checklist

Before finalizing a deck, verify:

1. **Color contrast**: Does body text contrast at least 4.5:1 with background?
2. **Font consistency**: Are there exactly 2 fonts (heading + body)?
3. **Layout variety**: Is any layout used three times in a row?
4. **Visual count**: Are there at least 3 visual slides?
5. **Motif consistency**: Is the motif the same on every slide?
6. **Background style**: Is the background style appropriate for the audience?
7. **Accent usage**: Are accents used only for emphasis, not decoration?
8. **White space**: Is there breathing room, or is every inch filled?

## Theme Examples

### Developer Tool (Dark)

```
background: #0a0c10
text: #E8EAF0
accent: #00ff9d
accent_2: #ff6b6b
muted: #6b7280
heading_font: Fira Code
body_font: Inter
background_style: glow
motif: lines
```

### Health App (Light)

```
background: #ffffff
text: #1f2937
accent: #10b981
accent_2: #3b82f6
muted: #9ca3af
heading_font: Montserrat
body_font: Inter
background_style: solid
motif: circles
```

### Enterprise SaaS (Neutral)

```
background: #f5f5f2
text: #2d2d2d
accent: #3d8b8b
accent_2: #c9a95e
muted: #6b6b6b
heading_font: Inter
body_font: Inter
background_style: gradient
motif: corner
```

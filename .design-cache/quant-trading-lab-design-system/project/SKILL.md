---
name: quant-trading-lab-design
description: Use this skill to generate well-branded interfaces and assets for Quant Trading Lab, either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping a dark-mode quantitative options research and backtesting workbench.
user-invocable: true
---

Read the README.md file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts or production code, depending on the need.

Key entry points:
- `README.md` — product context, content/voice rules, visual foundations, iconography
- `colors_and_type.css` — all design tokens (colors, type, spacing, radii, shadows) as CSS variables
- `preview/` — small preview cards demonstrating individual foundations
- `ui_kits/frontend/` — interactive React recreation of the workbench, with reusable JSX primitives (`Card`, `StatCard`, `Button`, `Input`, `Badge`, `Callout`, `TopNav`) and surface examples (`StrategyLab`, `OptionsChain`, `PortfolioDashboard`)

Core rules at a glance:
- Dark canvas `#0f1117`; surfaces layer by tone, not by shadow.
- Semantic color is mandatory: `#3b82f6` accent, `#00c896` bull, `#e5334e` bear, `#f59e0b` warn. No decorative hues.
- System sans stack for UI, `ui-monospace` for tickers/params/prices. Tabular nums for all numeric data.
- Sentence case headings. Imperative buttons. No emoji, ever — use PrimeIcons (`pi pi-*` from CDN).
- ITM options cells use a −45° hatch; ATM row gets an amber top/bottom border. These are the only decorative patterns in the system.

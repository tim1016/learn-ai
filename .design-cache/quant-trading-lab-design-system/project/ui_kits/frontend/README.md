# Quant Trading Lab — Frontend UI Kit

Interactive recreation of the Angular 21 + PrimeNG Aura-dark workbench. The kit is a click-through prototype — visuals and interactions only, no real data.

## Files

- `index.html` — workbench shell with a working Menubar + 3 switchable surfaces (Strategy Lab, Options Chain, Portfolio Dashboard)
- `TopNav.jsx` — the PrimeNG menubar recreation (Stocks / Data Quality / Options / Engine / Portfolio / Research Lab)
- `Primitives.jsx` — `Card`, `StatCard`, `Button`, `Input`, `Select`, `Checkbox`, `Badge`, `Eyebrow`, `Callout`
- `StrategyLab.jsx` — config card + hero metric strip + trade log
- `OptionsChain.jsx` — ITM/OTM/ATM options chain with hatched ITM cells and ATM row highlight
- `PortfolioDashboard.jsx` — 4-up stat strip + positions table + record-trade form

## Tokens

All visuals come from `/colors_and_type.css` at the project root — dark canvas, PrimeIcons from CDN, semantic bull/bear/warn/accent. No custom fonts.

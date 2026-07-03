# Bot Sprite Gallery Provenance

The bot sprite gallery uses repo-authored CSS pixel art generated from
`swordsman-sprite-pack.ts`. It does not depend on third-party sprite sheets or
vendored binary art assets.

The visual contract is:

- 64 px logical sprite frame size.
- Three generated palette levels.
- Four directional facings.
- Eight animation-state cards with explicit frame counts and durations.
- CSS pseudo-elements render the character, weapon, state pose, and animation.

The prior CraftPix PNG sheets were removed from this public repository because
their license permits use in projects but does not clearly permit public source
redistribution of raw PNG art files.

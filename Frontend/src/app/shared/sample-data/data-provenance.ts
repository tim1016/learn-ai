/**
 * Whether the numbers a view renders were measured (`real`) or generated for
 * demonstration (`sample`). The dataset carries it, so a view labels what it
 * renders rather than which service it happened to import (#3490 R28).
 */
export type DataProvenance = 'sample' | 'real';

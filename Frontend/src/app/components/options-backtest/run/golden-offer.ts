/**
 * What a run may be taken to Golden as, from where it is shown: the address to go to, or why it is not offered.
 * Taking a run to Golden carries the run's id and nothing else, and approves nothing; Golden reviews what that run froze.
 */
export type GoldenOffer =
  | { readonly kind: 'link'; readonly route: readonly string[] }
  | { readonly kind: 'withheld'; readonly why: string };

// Decoy file — every construct here LOOKS like something the
// analyzer extracts but must produce NO graph records. It exists so
// the accuracy benchmark measures precision under false-positive
// pressure: a regression that loosened detection would light these
// up and drop precision below the threshold.

export function arrayHelpers(items: number[]): number | undefined {
  // Array.prototype.find — "find" is a data-read verb, but the
  // receiver is a lowercase identifier, so the capitalised-model
  // gate rejects it. No data.items entity may be emitted.
  return items.find((x) => x === 1);
}

export function notAFetch(): void {
  // A commented-out call — the TS compiler drops comments, so no
  // contract may be emitted for this URL.
  // fetch("/api/legacy/should-not-appear");
}

export function builtins(values: number[]): number {
  // Capitalised receiver, but "max" is not a data verb — no match.
  return Math.max(...values);
}

export function prose(): string {
  // The word SELECT appears, but with no FROM/INTO clause there is
  // no table to extract, so no data entity may be emitted.
  return "remember to SELECT a sensible default here";
}

export function localFind(): number {
  // A locally-declared function literally named fetch — calling it
  // is not an HTTP contract.
  const fetch = (n: number): number => n + 1;
  return fetch(41);
}

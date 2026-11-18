export function tokensText(cssTexts: string[]): string;
export function buildTokens(cssTexts: string[]): {
  about: string;
  light: Record<string, string>;
  dark: Record<string, string>;
};
export function readBlocks(css: string): {
  light: Record<string, string>;
  dark: Record<string, string>;
};

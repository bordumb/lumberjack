/**
 * What both sides of the boundary need to agree on about the theme.
 *
 * The root layout is a server component and the provider is a client one; a shared
 * module is the only place a constant can live where both can read the value itself
 * rather than a reference to it.
 */

export type Theme = "light" | "dark";

/** Where the choice is kept. Read by the bootstrap script below and by the provider. */
export const THEME_KEY = "lj.theme";

/**
 * The dashboard has always painted dark, and that stays the default: a reader who never
 * opens settings sees exactly what they saw before.
 */
export const DEFAULT_THEME: Theme = "dark";

/**
 * The code surfaces highlight themselves rather than through the CSS tokens, so they
 * take the pair and a `themeType` saying which half is live.
 */
export const CODE_THEME = { light: "pierre-light", dark: "pierre-dark" } as const;

/**
 * Runs synchronously in the document head, before first paint.
 *
 * Server-rendered markup carries `dark` because the choice lives in localStorage, which
 * the server cannot read. Waiting for React to hydrate before correcting it would flash
 * the dark tree at every reader who picked light; this closes that window.
 */
export const THEME_BOOTSTRAP = `try{var t=localStorage.getItem(${JSON.stringify(THEME_KEY)});var d=t!=="light";document.documentElement.classList.toggle("dark",d);document.documentElement.style.colorScheme=d?"dark":"light"}catch(e){}`;

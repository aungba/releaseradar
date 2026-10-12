import createClient from 'openapi-fetch'
import type { paths } from './schema'

/**
 * Typed API client generated from the FastAPI OpenAPI schema (`npm run gen:api`).
 * Same-origin + credentials so the HttpOnly session cookie is sent automatically.
 */
export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: 'include',
  // Resolve fetch at call time (keeps tests able to stub it).
  fetch: (request) => globalThis.fetch(request),
})

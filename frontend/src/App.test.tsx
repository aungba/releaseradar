import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { App } from './App'

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

describe('App shell', () => {
  it('renders the comparator page', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ status: 'ok', database: true })))
    renderApp()
    expect(screen.getByRole('heading', { name: 'Release Comparator' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run Inspection' })).toBeDisabled()
  })

  it('shows API online when readiness succeeds', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ status: 'ok', database: true })))
    renderApp()
    expect(await screen.findByText('API online')).toBeInTheDocument()
  })

  it('shows API offline when the backend is down', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    renderApp()
    expect(await screen.findByText('API offline')).toBeInTheDocument()
  })
})

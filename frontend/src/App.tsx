import { Route, Routes } from 'react-router'
import { Layout } from './components/Layout'
import { ComparatorPage } from './features/comparator/ComparatorPage'

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<ComparatorPage />} />
      </Route>
    </Routes>
  )
}

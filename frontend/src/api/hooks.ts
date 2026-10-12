import { useQuery } from '@tanstack/react-query'
import { api } from './client'

export function useReadiness() {
  return useQuery({
    queryKey: ['readiness'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/ready')
      if (error || !data) throw new Error('API unreachable')
      return data
    },
    refetchInterval: 30_000,
    retry: false,
  })
}

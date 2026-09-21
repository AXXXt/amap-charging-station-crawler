import type { HistoryPoint, Meta, Overview, StationDetail, StationFilters, StationResponse } from './types'

async function request<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

export function fetchOverview() {
  return request<Overview>('/api/overview')
}

export function fetchStations(filters: StationFilters) {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== '' && value != null) params.set(key, String(value))
  })
  return request<StationResponse>(`/api/stations?${params.toString()}`)
}

export function fetchMeta() {
  return request<Meta>('/api/meta')
}

export function fetchStationDetail(sourceKey: string) {
  return request<StationDetail>(`/api/station?sourceKey=${encodeURIComponent(sourceKey)}`)
}

export function fetchStationHistory(sourceKey: string) {
  return request<{ items: HistoryPoint[] }>(`/api/station/history?sourceKey=${encodeURIComponent(sourceKey)}&limit=120`)
}

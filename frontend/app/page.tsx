'use client'

import dynamic from 'next/dynamic'
import { useMemo } from 'react'

const VenueMap = dynamic(() => import('../components/VenueMap').then(m => m.VenueMap), { ssr: false })

export default function Home() {
  const config = useMemo(() => ({
    apiKey: process.env.NEXT_PUBLIC_GOOGLE_MAPS_KEY ?? '',
    mapId: process.env.NEXT_PUBLIC_GOOGLE_MAPS_MAP_ID ?? '',
    defaultCenter: { lat: 40.7580, lng: -73.9855 },
    defaultZoom: 13,
  }), [])

  return (
    <main style={{ width: '100vw', height: '100vh', overflow: 'hidden' }}>
      <VenueMap config={config} userId="guest" />
    </main>
  )
}

'use client'

import dynamic from 'next/dynamic'

const VenueMap = dynamic(() => import('../components/VenueMap'), { ssr: false })

export default function Home() {
  const apiKey = process.env.NEXT_PUBLIC_GOOGLE_MAPS_KEY ?? ''
  const mapId = process.env.NEXT_PUBLIC_GOOGLE_MAPS_MAP_ID ?? ''

  return (
    <main style={{ width: '100vw', height: '100vh', overflow: 'hidden' }}>
      <VenueMap apiKey={apiKey} mapId={mapId} />
    </main>
  )
}

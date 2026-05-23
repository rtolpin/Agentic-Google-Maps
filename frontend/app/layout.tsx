import type { Metadata } from 'next'

export const metadata: Metadata = {
  title: 'The Right Spot',
  description: 'AI-powered venue discovery — find the perfect place for any activity',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, padding: 0, fontFamily: 'system-ui, sans-serif' }}>
        {children}
      </body>
    </html>
  )
}

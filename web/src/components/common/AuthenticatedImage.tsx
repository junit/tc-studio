import React, { useEffect, useState } from 'react'
import { fetchAuthenticatedBlob } from '@/lib/auth'

interface AuthenticatedImageProps extends React.ImgHTMLAttributes<HTMLImageElement> {
  src?: string
}

export const AuthenticatedImage: React.FC<AuthenticatedImageProps> = ({ src, ...props }) => {
  const [resolvedSrc, setResolvedSrc] = useState(src)

  useEffect(() => {
    let cancelled = false
    let objectUrl: string | undefined
    if (!src || src.startsWith('data:') || !src.startsWith('/api/')) {
      setResolvedSrc(src)
      return () => undefined
    }
    setResolvedSrc(undefined)
    fetchAuthenticatedBlob(src)
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setResolvedSrc(objectUrl)
      })
      .catch(() => {
        if (!cancelled) setResolvedSrc(undefined)
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [src])

  if (!resolvedSrc) return null
  return <img {...props} src={resolvedSrc} />
}

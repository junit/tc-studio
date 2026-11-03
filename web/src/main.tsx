import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { installApiAuthInterceptor } from './lib/auth'
import './index.css'

installApiAuthInterceptor()

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)

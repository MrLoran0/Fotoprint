import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import '@fontsource/manrope/400.css'
import '@fontsource/manrope/500.css'
import '@fontsource/manrope/600.css'
import '@fontsource/manrope/700.css'
import '@fontsource/manrope/800.css'
import './styles.css'
import './app.css'
import './print.css'

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)

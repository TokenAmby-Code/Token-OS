import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { MockOpsCockpit } from './MockOpsCockpit';
import './cockpit.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MockOpsCockpit />
  </StrictMode>,
);

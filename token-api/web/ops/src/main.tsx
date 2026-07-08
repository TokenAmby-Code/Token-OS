import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { OpsCockpit } from './OpsCockpit';
import './cockpit.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <OpsCockpit />
  </StrictMode>,
);

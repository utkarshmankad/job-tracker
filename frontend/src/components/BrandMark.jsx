export default function BrandMark({ size = 32 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="logo-grad" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#38bdf8"/>
          <stop offset="100%" stopColor="#0369a1"/>
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="7" fill="url(#logo-grad)"/>
      <path d="M12 15V12a4 4 0 0 1 8 0v3" stroke="white" strokeWidth="2.5" strokeLinecap="round"/>
      <rect x="4" y="15" width="24" height="13" rx="3" fill="white"/>
      <path d="M10 22.5l3.5 3.5 8.5-8.5" stroke="#0ea5e9" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

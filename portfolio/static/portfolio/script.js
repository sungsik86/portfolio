const revealElements = document.querySelectorAll('.reveal');

const observer = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  },
  { threshold: 0.15 }
);

revealElements.forEach((el, index) => {
  el.style.transitionDelay = `${index * 90}ms`;
  observer.observe(el);
});

const clock = document.getElementById('clock');

function updateClock() {
  if (!clock) return;
  const now = new Date();
  clock.textContent = now.toLocaleTimeString('ko-KR', { hour12: false });
}

updateClock();
setInterval(updateClock, 1000);

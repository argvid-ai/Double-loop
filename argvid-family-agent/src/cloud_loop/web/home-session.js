if (location.protocol === 'https:') {
  fetch('/access/session').then(response => response.ok ? response.json() : null).then(session => {
    if (!session?.authenticated) return;
    const button = document.getElementById('homeLogout');
    button.hidden = false;
    button.addEventListener('click', async () => {
      const response = await fetch('/access/logout', {method: 'POST'});
      if (response.ok) location.replace('/access/login');
    });
  }).catch(() => {});
}

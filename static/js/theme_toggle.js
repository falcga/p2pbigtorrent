(function () {
    const savedTheme = localStorage.getItem('theme') || 'light';
    document.body.setAttribute('data-theme', savedTheme);

    document.addEventListener('DOMContentLoaded', function () {
        const toggleBtn = document.getElementById('theme-toggle');
        if (!toggleBtn) return;
        toggleBtn.addEventListener('click', function () {
            const currentTheme = document.body.getAttribute('data-theme') || 'light';
            const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
            document.body.setAttribute('data-theme', nextTheme);
            localStorage.setItem('theme', nextTheme);
        });
    });
})();

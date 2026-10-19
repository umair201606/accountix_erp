document.addEventListener('DOMContentLoaded', function () {
    // Sidebar toggle (desktop: collapse, mobile: open/close)
    const toggle = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    const isMobile = () => window.innerWidth <= 768;

    if (toggle && sidebar) {
        toggle.addEventListener('click', () => {
            if (isMobile()) {
                sidebar.classList.toggle('open');
                if (overlay) overlay.classList.toggle('show');
            } else {
                sidebar.classList.toggle('collapsed');
            }
        });
    }
    if (overlay) {
        overlay.addEventListener('click', () => {
            sidebar.classList.remove('open');
            overlay.classList.remove('show');
        });
    }
    window.addEventListener('resize', () => {
        if (!isMobile()) {
            sidebar.classList.remove('open');
            if (overlay) overlay.classList.remove('show');
        }
    });

    // ── Collapsible Nav Groups: an accordion ──
    // One group open at a time. On load only the group holding the current
    // page is open (the server marks it .expanded) — nothing else opens by
    // itself. Each group's state used to be remembered separately in
    // localStorage, so every group ever opened kept re-opening on every page
    // until the whole sidebar was expanded.
    const navGroups = Array.from(document.querySelectorAll('.nav-group'));
    try {
        Object.keys(localStorage).forEach(k => { if (k.indexOf('sidebar_group_') === 0) localStorage.removeItem(k); });
    } catch (e) { /* storage blocked: nothing to clean */ }
    const setOpen = (group, open) => {
        group.classList.toggle('expanded', open);
        const header = group.querySelector('.nav-group-header');
        if (header) header.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    let first = true;
    navGroups.forEach(group => {
        // At most one group starts open: the first that holds the active page.
        const open = group.classList.contains('expanded') && first;
        if (open) first = false;
        setOpen(group, open);
        const header = group.querySelector('.nav-group-header');
        if (!header) return;
        header.setAttribute('role', 'button');
        header.setAttribute('tabindex', '0');
        const toggle = (e) => {
            e.stopPropagation();
            const willOpen = !group.classList.contains('expanded');
            navGroups.forEach(g => { if (g !== group) setOpen(g, false); });
            setOpen(group, willOpen);
        };
        header.addEventListener('click', toggle);
        header.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); }
        });
    });

    // Notification dropdown
    const notifBtn = document.getElementById('notif-btn');
    const notifMenu = document.getElementById('notif-menu');
    if (notifBtn && notifMenu) {
        notifBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            notifMenu.classList.toggle('hidden');
        });
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.notif-dropdown')) {
                notifMenu.classList.add('hidden');
            }
        });
    }

    // Mark notification read
    document.querySelectorAll('.notif-item').forEach(item => {
        item.addEventListener('click', function () {
            const id = this.dataset.id;
            if (id) {
                fetch(`/auth/api/notifications/${id}/read`, { method: 'POST' });
                this.remove();
                const badge = document.querySelector('.notif-badge');
                if (badge) {
                    const count = parseInt(badge.textContent) - 1;
                    badge.textContent = count;
                    if (count <= 0) badge.remove();
                }
            }
        });
    });

    // Mark all read
    const markAllBtn = document.getElementById('mark-all-read');
    if (markAllBtn) {
        markAllBtn.addEventListener('click', () => {
            fetch('/auth/api/notifications/read-all', { method: 'POST' }).then(() => {
                document.querySelectorAll('.notif-item').forEach(el => el.remove());
                const badge = document.querySelector('.notif-badge');
                if (badge) badge.remove();
            });
        });
    }

    // Auto-close flash messages
    document.querySelectorAll('.flash-success, .flash-danger, .flash-warning, .flash-info').forEach(el => {
        setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.5s'; }, 4000);
        setTimeout(() => { el.remove(); }, 4500);
    });
});

// One submit per form at a time. A double-clicked Save posted the same form
// twice — two customers, two products, a second approval racing the first.
// Registered on window, so it runs after every page handler: a form whose
// own handler cancels the submit (it saves with fetch) is left to it. The
// guard lifts when the user comes back to the page (back button), and after
// a few seconds in case the answer was a file download rather than a page.
window.addEventListener('submit', (e) => {
    const form = e.target;
    if (e.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
    if ((form.target && form.target !== '_self') || 'allowResubmit' in form.dataset) return;
    if (form.dataset.submitting === '1') { e.preventDefault(); return; }
    form.dataset.submitting = '1';
    setTimeout(() => { delete form.dataset.submitting; }, 8000);
});
window.addEventListener('pageshow', () => {
    document.querySelectorAll('form[data-submitting]').forEach((f) => { delete f.dataset.submitting; });
});

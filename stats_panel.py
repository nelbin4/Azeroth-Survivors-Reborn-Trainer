"""Stats table and selected-stat editor."""
import time
import tkinter as tk
from tkinter import font, ttk

from stats_controller import StatsController
from stats_model import BY_KEY, VIEWS


class StatsPanel:
    def __init__(self, root, parent, session_provider, native_provider, closing, preferences=None, save_preferences=lambda values: None):
        self.root, self.closing = root, closing
        self.selected = None
        self.advanced = None
        self.connection_state = None
        self.search_session = None
        self.search_started = None
        self.progress_running = False
        self.save_preferences = save_preferences
        self.controller = StatsController(root, session_provider, native_provider, closing, self.refresh, preferences)

        workspace = ttk.Frame(parent, padding=(10, 10, 10, 12))
        workspace.pack(fill='both', expand=True)
        workspace.columnconfigure(0, weight=1)
        workspace.columnconfigure(1, minsize=245)
        workspace.rowconfigure(0, weight=1)
        listing = ttk.Frame(workspace)
        listing.grid(row=0, column=0, sticky='nsew')
        editor = ttk.Frame(workspace, padding=(14, 0, 0, 0))
        editor.grid(row=0, column=1, sticky='nsew')

        self.find_button = ttk.Button(editor, text='Find', style='Find.TButton',
                                      command=self.toggle_search)
        self.find_button.pack(fill='x')
        self.pause = ttk.Button(editor, command=self.controller.pause)
        self.pause.pack(fill='x', pady=(6, 0))
        self.search_progress = ttk.Progressbar(editor, mode='indeterminate',
                                               orient='horizontal', maximum=100)
        self.search_progress.pack(fill='x', pady=(6, 8))
        self.summary = tk.StringVar()
        ttk.Label(editor, textvariable=self.summary, foreground='#76dfc1',
                  wraplength=229).pack(anchor='w')
        self.search_hint = tk.StringVar()
        ttk.Label(editor, textvariable=self.search_hint, foreground='#a9b8c8',
                  wraplength=229).pack(anchor='w')
        ttk.Separator(editor).pack(fill='x', pady=12)

        columns = ('stat', 'current', 'target', 'state')
        self.table = ttk.Treeview(listing, columns=columns, show='tree headings', selectmode='browse')
        style = ttk.Style(root)
        self.row_font = font.Font(root=root, family='Segoe UI', size=12)
        style.configure('Stats.Treeview.Heading', font=('Segoe UI', 11, 'bold'),
                        background='#213243', foreground='#e7eef5', relief='flat', padding=(4, 4))
        style.map('Stats.Treeview.Heading', background=[('active', '#293f54')])
        style.configure('Stats.Treeview', background='#14202c', fieldbackground='#14202c',
                        foreground='#e5e9ef', font=self.row_font,
                        rowheight=max(30, self.row_font.metrics('linespace') + 8))
        style.map('Stats.Treeview', background=[('selected', '#23443f')],
                  foreground=[('selected', '#ffffff')])
        self.table.configure(style='Stats.Treeview')
        self.table.heading('#0', text='Freeze', anchor='center')
        self.table.column('#0', width=76, minwidth=76, stretch=False, anchor='center')
        self.checkbox_images = {enabled: self.make_checkbox(enabled) for enabled in (False, True)}
        for column, width, anchor in zip(columns, (175, 95, 105, 109), ('w', 'e', 'e', 'w')):
            self.table.heading(column, text='Status' if column == 'state' else column.title(), anchor=anchor)
            self.table.column(column, width=width, minwidth=48, anchor=anchor, stretch=column == 'stat')
        for view in VIEWS:
            self.table.insert('', 'end', iid=view.key, values=(view.label,))
        self.table.pack(side='left', fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self.selection_changed)
        self.table.bind('<Button-1>', self.toggle_row_enabled)
        self.table.bind('<space>', self.toggle_selected_enabled)

        self.title = tk.StringVar()
        ttk.Label(editor, textvariable=self.title, font=('Segoe UI', 14, 'bold'), wraplength=235).pack(anchor='w')
        self.live = tk.StringVar()
        ttk.Label(editor, textvariable=self.live, font=('Segoe UI', 20, 'bold'),
                  foreground='#76dfc1').pack(anchor='w', pady=(2, 12))
        self.target_label = tk.StringVar()
        ttk.Label(editor, textvariable=self.target_label).pack(anchor='w')
        self.target = tk.StringVar()
        self.entry = ttk.Entry(editor, textvariable=self.target, width=20)
        self.entry.pack(anchor='w', pady=(4, 6))
        self.entry.bind('<Return>', lambda event: self.apply())
        actions = ttk.Frame(editor)
        actions.pack(anchor='w')
        self.apply_button = ttk.Button(actions, style='Accent.TButton', command=self.apply)
        self.apply_button.pack(side='left')
        self.maximum_button = ttk.Button(actions, text='Use maximum', command=self.use_maximum)
        self.maximum_button.pack(side='left', padx=(6, 0))
        self.help = tk.StringVar()
        self.error = tk.StringVar()
        ttk.Label(editor, textvariable=self.help, wraplength=228, foreground='#a9b8c8').pack(anchor='w', pady=(10, 0))
        ttk.Label(editor, textvariable=self.error, wraplength=228, foreground='#efaaaa').pack(anchor='w')
        self.select('damage')

    def toggle_search(self):
        if self.controller.policy.searching:
            self.controller.stop_search()
        else:
            self.controller.find()

    def make_checkbox(self, enabled):
        image = tk.PhotoImage(master=self.root, width=24, height=24)
        fill = '#56d6b0' if enabled else '#182838'
        outline = '#56d6b0' if enabled else '#8299ac'
        for y, inset in enumerate((4, 2, 1, 1) + (0,) * 16 + (1, 1, 2, 4)):
            image.put(outline, to=(inset, y, 24-inset, y+1))
            if 0 < y < 23:
                inner = max(inset + 1, (2 if y in (1, 22) else 0))
                image.put(fill, to=(inner, y, 24-inner, y+1))
        if enabled:
            for x, y in ((5, 12), (6, 13), (7, 14), (8, 15), (9, 16),
                         (10, 15), (11, 14), (12, 13), (13, 12), (14, 11),
                         (15, 10), (16, 9), (17, 8), (18, 7)):
                image.put('#10251f', to=(x, y, x+2, y+2))
        return image

    def selection_changed(self, _event):
        selection = self.table.selection()
        if selection and selection[0] != self.selected:
            self.select(selection[0])

    def select(self, key):
        self.table.selection_set(key)
        self.table.focus(key)
        self.table.see(key)
        if self.selected != key:
            self.selected = key
            self.load_target()
        self.refresh()

    def load_target(self):
        policy = self.controller.policy
        self.target.set('Match maximum' if self.selected == 'health' else str(policy.effective_target(self.selected)))
        self.error.set('')

    def toggle_row_enabled(self, event):
        if self.table.identify_region(event.x, event.y) == 'tree' and self.table.identify_column(event.x) == '#0':
            key = self.table.identify_row(event.y)
            if key:
                self.table.focus_set()
                self.select(key)
                return self.toggle_selected_enabled(event)

    def toggle_selected_enabled(self, _event):
        controller = self.controller
        policy = controller.policy
        enabled = self.selected not in policy.enabled
        policy.set_enabled(self.selected, enabled)
        self.load_target()
        self.persist_preferences()
        if enabled and not policy.paused and not policy.searching and self.selected not in policy.verified:
            if controller.connected_session():
                controller.find()
                return 'break'
        controller.changed()
        return 'break'

    def persist_preferences(self):
        try:
            self.save_preferences(self.controller.policy.preferences())
        except OSError as exc:
            self.error.set(f'Could not save stat preferences: {exc}')

    def apply(self):
        controller = self.controller
        policy = controller.policy
        try:
            policy.set_target(self.selected, self.target.get())
        except (ValueError, OverflowError) as exc:
            self.error.set(str(exc))
            return
        self.error.set('')
        self.persist_preferences()
        if not policy.searching and self.selected not in policy.verified and controller.connected_session():
            controller.find()
        else:
            controller.changed()

    def use_maximum(self):
        view = BY_KEY[self.selected]
        self.target.set('Match maximum' if self.selected == 'health' else str(view.maximum))
        self.error.set('')

    def refresh_connection(self):
        state = (self.controller.session_provider(), self.controller.connected_session())
        if state != self.connection_state:
            self.connection_state = state
            self.refresh()
        else:
            self.refresh_search_progress(state[1])

    def refresh_search_progress(self, session):
        controller = self.controller
        active = session is not None and controller.policy.searching
        if not active or session != self.search_session:
            self.search_session = session
            self.search_started = time.monotonic() if active else None
        if not active:
            self.search_started = None
        elif self.search_started is None:
            self.search_started = time.monotonic()

        found = active and bool(controller.policy.verified)
        result = controller.last
        blocked = bool(result.get('array_error') or (result.get('unmapped') and not found))
        checking_role = found and result.get('role', controller.role) == 'unknown'
        working = active and not blocked and (not found or checking_role)
        if working != self.progress_running:
            self.search_progress.stop()
            self.progress_running = working
            if working:
                self.search_progress.configure(mode='indeterminate', value=0)
                self.search_progress.start(18)
            else:
                self.search_progress.configure(mode='determinate')
        if not working:
            self.search_progress.configure(value=100 if found else 0)
            hint = ''
            if active and result.get('array_error'):
                hint = 'Waiting for a different game build.'
            elif active and blocked:
                hint = 'See Advanced for detection details.'
            elif found and result.get('unmapped'):
                hint = 'Hunt continues to monitor verified stats.'
            elif found:
                hint = 'Stats found. Hunt is monitoring in the background.'
            self.search_hint.set(hint)
            return

        elapsed = int(time.monotonic() - self.search_started)
        timer = f'{elapsed // 60}:{elapsed % 60:02d} elapsed'
        if checking_role:
            stage = 'Stats found. Checking session role'
        else:
            stage = 'Searching for stats'
        expectation = 'Usually takes 1–3 minutes' if elapsed < 180 else 'Still working'
        self.search_hint.set(f'{stage} · {expectation} · {timer}')

    def refresh(self):
        if self.closing():
            return
        controller = self.controller
        policy, last = controller.policy, controller.last
        session = controller.session_provider()
        connected_session = controller.connected_session()
        connected = connected_session is not None
        self.refresh_search_progress(connected_session)
        message = controller.message if connected else 'Connecting to Reborn…' if session else 'Start Reborn to find stats.'
        self.summary.set(message)
        self.find_button.configure(text='Stop search' if policy.searching and connected else 'Find',
                                   state='normal' if connected else 'disabled')
        self.pause.configure(text='Resume Freeze' if policy.paused else 'Stop Freeze',
                             state='normal' if connected and (policy.paused or policy.searching or policy.holds) else 'disabled')

        freeze = last.get('freeze', {})
        role = last.get('role', controller.role)
        read_only = role in ('unknown', 'guest') or last.get('host_required')
        for view in VIEWS:
            key = view.key
            frozen = key in policy.holds
            known = key in policy.verified
            target = 'Full' if key == 'health' else f'{policy.effective_target(key):,}{view.unit}'
            if key == 'max_health':
                target = '≥ ' + target
            state = 'Searching' if policy.searching else 'Not found'
            enabled = key in policy.enabled
            if key in last.get('unmapped', ()):
                state = 'Unavailable'
            elif not enabled and key not in policy.pending:
                state = 'Manual'
            elif read_only and known:
                state = 'Checking role' if role == 'unknown' else 'Read-only'
            elif policy.paused and enabled:
                state = 'Stopped'
            elif key in freeze.get('errors', {}):
                state = 'Retrying' if known else 'Waiting'
            elif key in policy.pending:
                state = 'Queued'
            elif known:
                state = 'Ready'
                if frozen:
                    state = 'Frozen' if key in freeze.get('confirmed', {}) else 'Verifying'
            values = (view.label, view.display(policy.values.get(key)), target, state)
            image = str(self.checkbox_images[enabled])
            if tuple(self.table.item(key, 'values')) != values or self.table.item(key, 'image') != (image,):
                self.table.item(key, image=image, values=values)

        view = BY_KEY[self.selected]
        self.title.set(view.label)
        self.live.set(view.display(policy.values.get(view.key)))
        self.target_label.set('Target' + (f' ({view.unit})' if view.unit else ''))
        enabled = view.key in policy.enabled
        self.entry.configure(state='normal' if not enabled and view.key != 'health' else 'disabled')
        self.maximum_button.configure(state='normal' if not enabled and view.key != 'health' else 'disabled')
        self.help.set(freeze.get('errors', {}).get(view.key) or view.note
                      or f'Range {view.minimum:,}–{view.maximum:,}{view.unit}')
        if enabled:
            self.help.set(freeze.get('errors', {}).get(view.key) or 'Checked stats freeze at maximum.')
        can_apply = view.key in policy.verified and not read_only
        unavailable = view.key in last.get('unmapped', ())
        if unavailable:
            self.help.set('Hunt could not verify this stat on the current build.')
        self.apply_button.configure(text='Apply' if can_apply else 'Queue target',
                                    state='disabled' if enabled or unavailable else 'normal')

        if self.advanced is not None and self.advanced.winfo_exists():
            self.refresh_detection_details(connected_session)

    def refresh_detection_details(self, session):
        controller = self.controller
        policy = controller.policy
        result = controller.last
        character = result['heroes'][0] if len(result.get('heroes', ())) == 1 else None
        if character:
            name = character.get('name') or 'Selected character'
            if character.get('level') is not None:
                name += f" · Level {character['level']}"
        else:
            name = 'Waiting for a verified character' if policy.searching else '—'
        roles = {'host': 'Hosting multiplayer', 'guest': 'Guest · read-only',
                 'none': 'Solo', 'unknown': 'Checking session role'}
        freeze = result.get('freeze', {})
        verified = policy.verified & policy.enabled
        frozen = freeze.get('confirmed', {}).keys() & verified & policy.holds
        fields = {
            'Game process': f'PID {session.pid}' if session else 'Disconnected',
            'Build': result.get('build') or '—',
            'Player array': 'Unavailable' if result.get('array_error') else result.get('method', '—'),
            'Character': name,
            'Session': roles.get(controller.role, 'Checking session role') if session else '—',
            'Verified stats': f'{len(policy.verified)} of {len(VIEWS)}',
            'Frozen': f'{len(frozen)} of {len(policy.enabled)} enabled',
        }
        lines = [self.summary.get(), '']
        lines.extend(f'{label}: {value}' for label, value in fields.items())
        errors = freeze.get('errors', {})
        warnings = [f'{BY_KEY[key].label}: {message}' for key, message in errors.items()
                    if key in policy.enabled or key in policy.pending]
        if result.get('array_error'):
            warnings.insert(0, result['array_error'])
        if result.get('ambiguous'):
            warnings.insert(0, 'Multiple player allocations found. Writes are blocked until the character is verified.')
        if warnings:
            lines.extend(('', 'Warnings', *warnings))
        lines.extend(('', f"{'Stat':25} {'Current':>12} {'Target':>12}  Status"))
        for view in VIEWS:
            label, current, target, status = self.table.item(view.key, 'values')
            lines.append(f'{label:25} {current:>12} {target:>12}  {status}')
        content = '\n'.join(lines)
        if content != self.detection_content:
            position = self.diagnostics.yview()[0]
            self.diagnostics.configure(state='normal')
            self.diagnostics.delete('1.0', 'end')
            self.diagnostics.insert('1.0', content)
            self.diagnostics.configure(state='disabled')
            self.diagnostics.yview_moveto(position)
            self.detection_content = content

    def copy_detection_details(self):
        self.advanced.clipboard_clear()
        self.advanced.clipboard_append(self.detection_content)
        self.copy_detection_button.configure(text='Copied')

    def build_detection_details(self, parent):
        self.detection_content = ''
        header = ttk.Frame(parent)
        header.pack(fill='x', pady=(0, 8))
        ttk.Label(header, text='Detection details', font=('Segoe UI', 16, 'bold')).pack(side='left')
        self.copy_detection_button = ttk.Button(header, text='Copy details', command=self.copy_detection_details)
        self.copy_detection_button.pack(side='right')
        self.diagnostics = tk.Text(parent, font=('Consolas', 11), wrap='word',
                                   padx=12, pady=10, background='#0c1826',
                                   foreground='#e5e9ef', state='disabled')
        self.diagnostics.pack(fill='both', expand=True)

    def open_advanced(self):
        if self.advanced is not None and self.advanced.winfo_exists():self.advanced.lift();return
        from value_hunt import add_manual_panel
        window=tk.Toplevel(self.root);self.advanced=window
        window.title('Stats · Advanced');window.geometry('980x680')
        tabs=ttk.Notebook(window);tabs.pack(fill='both',expand=True)
        details = ttk.Frame(tabs, padding=16)
        tabs.add(details, text='Detection details')
        self.build_detection_details(details)
        manual=ttk.Frame(tabs,padding=16);tabs.add(manual,text='Manual value search')
        add_manual_panel(self.root,manual,self.controller.session_provider,lambda:self.closing() or not window.winfo_exists())
        tabs.select(details)
        self.refresh()

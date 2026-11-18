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
        self.table.heading('#0', text='Enabled', anchor='center')
        self.table.column('#0', width=76, minwidth=76, stretch=False, anchor='center')
        self.checkbox_images = {enabled: self.make_checkbox(enabled) for enabled in (False, True)}
        for column, width, anchor in zip(columns, (175, 95, 105, 109), ('w', 'e', 'e', 'w')):
            self.table.heading(column, text='Status' if column == 'state' else column.title(), anchor=anchor)
            self.table.column(column, width=width, minwidth=48, anchor=anchor, stretch=column == 'stat')
        for view in VIEWS:
            self.table.insert('', 'end', iid=view.key, values=(view.label,))
        scroll = ttk.Scrollbar(listing, orient='vertical', command=self.table.yview)
        scroll.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scroll.set)
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
        self.entry.bind('<Return>', lambda event: self.apply(self.editor_frozen.get()))
        self.editor_frozen = tk.BooleanVar()
        self.freeze_edited = False
        self.editor_check = ttk.Checkbutton(editor, text='Keep frozen', variable=self.editor_frozen,
                                            command=self.edit_freeze)
        self.editor_check.pack(anchor='w', pady=(0, 8))
        actions = ttk.Frame(editor)
        actions.pack(anchor='w')
        self.apply_button = ttk.Button(actions, style='Accent.TButton',
                                       command=lambda: self.apply(self.editor_frozen.get()))
        self.apply_button.pack(side='left')
        self.maximum_button = ttk.Button(actions, text='Use maximum', command=self.use_maximum)
        self.maximum_button.pack(side='left', padx=(6, 0))
        self.help = tk.StringVar()
        self.error = tk.StringVar()
        ttk.Label(editor, textvariable=self.help, wraplength=228, foreground='#a9b8c8').pack(anchor='w', pady=(10, 0))
        ttk.Label(editor, textvariable=self.error, wraplength=228, foreground='#efaaaa').pack(anchor='w')
        self.xp = tk.BooleanVar(value=False)
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

    def edit_freeze(self):
        self.freeze_edited = True

    def load_target(self):
        self.freeze_edited = False
        policy = self.controller.policy
        self.target.set('Match maximum' if self.selected == 'health' else str(policy.targets[self.selected]))
        self.editor_frozen.set(self.selected in policy.holds)
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
        self.error.set('')
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

    def apply(self, freeze):
        policy = self.controller.policy
        try:
            policy.set_target(self.selected, self.target.get(), freeze)
        except (ValueError, OverflowError) as exc:
            self.error.set(str(exc))
            return
        self.freeze_edited = False
        self.error.set('')
        self.persist_preferences()
        self.controller.changed()

    def use_maximum(self):
        view = BY_KEY[self.selected]
        self.target.set('Match maximum' if self.selected == 'health' else str(view.maximum))
        self.error.set('')

    def change_xp(self):
        self.controller.policy.xp = self.xp.get()
        self.controller.changed()

    def once_xp(self):
        self.controller.policy.xp_once = True
        self.controller.changed()

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
        checking_role = found and controller.role == 'unknown'
        working = active and (not found or checking_role)
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
            self.search_hint.set('Stats found. Hunt is monitoring in the background.' if found else '')
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
        read_only = controller.role in ('unknown', 'guest') or last.get('host_required')
        for view in VIEWS:
            key = view.key
            frozen = key in policy.holds
            known = key in policy.verified
            target = 'Full' if key == 'health' else f'{policy.targets[key]:,}{view.unit}'
            if key == 'max_health':
                target = '≥ ' + target
            state = 'Searching' if policy.searching else 'Not found'
            enabled = key in policy.enabled
            if not enabled:
                state = 'Disabled'
            elif read_only and known:
                state = 'Checking role' if controller.role == 'unknown' else 'Read-only'
            elif policy.paused and (frozen or key in policy.pending):
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
        if not self.freeze_edited:
            self.editor_frozen.set(view.key in policy.holds)
        self.title.set(view.label)
        self.live.set(view.display(policy.values.get(view.key)))
        self.target_label.set('Target' + (f' ({view.unit})' if view.unit else ''))
        enabled = view.key in policy.enabled
        self.entry.configure(state='normal' if enabled and view.key != 'health' else 'disabled')
        self.editor_check.configure(state='normal' if enabled else 'disabled')
        self.maximum_button.configure(state='normal' if enabled else 'disabled')
        self.help.set(freeze.get('errors', {}).get(view.key) or view.note
                      or f'Range {view.minimum:,}–{view.maximum:,}{view.unit}')
        if not enabled:
            self.help.set('Check Enabled in the table to apply a target.')
        can_apply = view.key in policy.verified and not (policy.paused or read_only)
        self.apply_button.configure(text='Apply' if can_apply else 'Queue target',
                                    state='normal' if enabled else 'disabled')

        if self.advanced is not None and self.advanced.winfo_exists():
            xp_available = bool(last.get('xp_available'))
            for control in (self.xp_check, self.xp_button):
                control.configure(state='normal' if xp_available else 'disabled')
            self.xp_status.set('XP controls require a verified solo layout.' if not xp_available else '')
            self.diagnostics.configure(state='normal')
            self.diagnostics.delete('1.0', 'end')
            self.diagnostics.insert('end', repr(last) if last else controller.message)
            self.diagnostics.configure(state='disabled')

    def open_advanced(self):
        if self.advanced is not None and self.advanced.winfo_exists():self.advanced.lift();return
        from value_hunt import add_manual_panel
        window=tk.Toplevel(self.root);self.advanced=window
        window.title('Stats · Advanced');window.geometry('980x680')
        tabs=ttk.Notebook(window);tabs.pack(fill='both',expand=True)
        manual=ttk.Frame(tabs,padding=16);tabs.add(manual,text='Manual value search')
        add_manual_panel(self.root,manual,self.controller.session_provider,lambda:self.closing() or not window.winfo_exists())
        actions = ttk.Frame(tabs, padding=16)
        tabs.add(actions, text='XP')
        self.xp_check = ttk.Checkbutton(actions, text='XP assistance', variable=self.xp,
                                       command=self.change_xp)
        self.xp_check.pack(anchor='w', pady=(0, 8))
        self.xp_button = ttk.Button(actions, text='One XP from level-up', command=self.once_xp)
        self.xp_button.pack(anchor='w')
        self.xp_status = tk.StringVar()
        ttk.Label(actions, textvariable=self.xp_status).pack(anchor='w', pady=8)
        debug=ttk.Frame(tabs,padding=16);tabs.add(debug,text='Detection details')
        self.diagnostics=tk.Text(debug,wrap='word',background='#0c1826',foreground='#e5e9ef',state='disabled')
        self.diagnostics.pack(fill='both',expand=True);self.refresh()

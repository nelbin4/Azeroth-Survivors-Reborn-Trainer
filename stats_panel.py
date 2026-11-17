"""Stats table and selected-stat editor."""
import time
import tkinter as tk
from tkinter import ttk

from stats_controller import StatsController
from stats_model import BY_KEY, VIEWS


class StatsPanel:
    def __init__(self, root, parent, session_provider, native_provider, closing, on_options=lambda result: None):
        self.root, self.closing = root, closing
        self.selected = None
        self.advanced = None
        self.connection_state = None
        self.search_session = None
        self.search_started = None
        self.progress_running = False
        self.controller = StatsController(root, session_provider, native_provider, closing, self.refresh, on_options)

        top = ttk.Frame(parent, padding=(14, 12, 14, 8))
        top.pack(fill='x')
        search_controls = ttk.Frame(top)
        search_controls.pack(side='left', anchor='n')
        self.find_button = ttk.Button(search_controls, text='Find', width=13,
                                      style='Find.TButton', command=self.toggle_search)
        self.find_button.pack(fill='x')
        self.search_progress = ttk.Progressbar(search_controls, mode='indeterminate',
                                                orient='horizontal', maximum=100)
        self.search_progress.pack(fill='x', pady=(5, 0))
        feedback = ttk.Frame(top)
        feedback.pack(side='left', padx=12)
        self.summary = tk.StringVar()
        ttk.Label(feedback, textvariable=self.summary, foreground='#76dfc1',
                  wraplength=700).pack(anchor='w')
        self.search_hint = tk.StringVar()
        ttk.Label(feedback, textvariable=self.search_hint, foreground='#a9b8c8').pack(anchor='w')

        toolbar = ttk.Frame(parent, padding=(14, 0, 14, 8))
        toolbar.pack(fill='x')
        self.auto = tk.BooleanVar(value=True)
        ttk.Style(root).configure('StatsAuto.TCheckbutton', font=('Segoe UI', 12))
        ttk.Checkbutton(toolbar, text='Auto max & freeze', variable=self.auto,
                        command=self.change_auto, style='StatsAuto.TCheckbutton').pack(side='left')
        self.role_text = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.role_text).pack(side='left', padx=14)
        self.pause = ttk.Button(toolbar, command=self.controller.pause)
        self.pause.pack(side='right')
        self.max_all = ttk.Button(toolbar, text='Max & freeze all', command=self.maximize)
        self.max_all.pack(side='right', padx=8)

        workspace = ttk.Frame(parent, padding=(14, 0, 14, 12))
        workspace.pack(fill='both', expand=True)
        editor = ttk.Frame(workspace, padding=(18, 0, 0, 0), width=275)
        editor.pack(side='right', fill='y')
        listing = ttk.Frame(workspace)
        listing.pack(side='left', fill='both', expand=True)
        filters = ttk.Frame(listing)
        filters.pack(fill='x', pady=(0, 6))
        self.category = tk.StringVar(value='All')
        for name in ('All', 'Offense', 'Defense', 'Utility', 'Resources'):
            ttk.Radiobutton(filters, text=name, value=name, variable=self.category,
                            command=self.filter_rows).pack(side='left', padx=(0, 8))

        columns = ('stat', 'current', 'target', 'freeze', 'state')
        self.table = ttk.Treeview(listing, columns=columns, show='headings', selectmode='browse')
        style = ttk.Style(root)
        style.configure('Stats.Treeview', background='#14202c', fieldbackground='#14202c',
                        foreground='#e5e9ef', rowheight=26)
        style.map('Stats.Treeview', background=[('selected', '#23443f')],
                  foreground=[('selected', '#ffffff')])
        self.table.configure(style='Stats.Treeview')
        for column, width, anchor in zip(columns, (185, 92, 98, 63, 104),
                                         ('w', 'e', 'e', 'center', 'w')):
            self.table.heading(column, text=column.title(), anchor=anchor)
            self.table.column(column, width=width, minwidth=48, anchor=anchor, stretch=column == 'stat')
        for view in VIEWS:
            self.table.insert('', 'end', iid=view.key, values=(view.label,))
        scroll = ttk.Scrollbar(listing, orient='vertical', command=self.table.yview)
        scroll.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left', fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self.selection_changed)
        self.table.bind('<Button-1>', self.toggle_row_freeze)
        self.table.bind('<space>', self.toggle_selected_freeze)

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
        ttk.Button(actions, text='Use maximum', command=self.use_maximum).pack(side='left', padx=8)
        self.help = tk.StringVar()
        self.error = tk.StringVar()
        ttk.Label(editor, textvariable=self.help, wraplength=228, foreground='#a9b8c8').pack(anchor='w', pady=(10, 0))
        ttk.Label(editor, textvariable=self.error, wraplength=228, foreground='#efaaaa').pack(anchor='w')
        ttk.Button(editor, text='Advanced…', command=self.open_advanced).pack(side='bottom', anchor='w')
        self.xp = tk.BooleanVar(value=False)
        self.select('damage')

    def toggle_search(self):
        if self.controller.policy.searching:
            self.controller.stop_search()
        else:
            self.controller.find()

    def filter_rows(self):
        group = self.category.get()
        visible = [view.key for view in VIEWS if group in ('All', view.group)]
        self.table.set_children('', *visible)
        self.select(self.selected if self.selected in visible else visible[0])

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

    def toggle_row_freeze(self, event):
        if self.table.identify_region(event.x, event.y) == 'cell' and self.table.identify_column(event.x) == '#4':
            key = self.table.identify_row(event.y)
            if key:
                self.select(key)
                return self.toggle_selected_freeze(event)

    def toggle_selected_freeze(self, _event):
        policy = self.controller.policy
        if self.selected in policy.verified:
            enabled = self.selected not in policy.holds
            self.freeze_edited = False
            self.editor_frozen.set(enabled)
            policy.freeze(self.selected, enabled)
            self.controller.changed()
        return 'break'

    def change_auto(self):
        self.controller.policy.auto = self.auto.get()
        self.controller.changed()

    def maximize(self):
        self.controller.maximize()
        self.load_target()

    def apply(self, freeze):
        policy = self.controller.policy
        try:
            policy.set_target(self.selected, self.target.get(), freeze)
        except (ValueError, OverflowError) as exc:
            self.error.set(str(exc))
            return
        self.freeze_edited = False
        self.error.set('')
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
        checking_options = found and controller.options_pending
        working = active and (not found or checking_role or checking_options)
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
        elif checking_options:
            stage = 'Stats found. Checking Run options'
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
        self.role_text.set({
            'host': 'Host',
            'guest': 'Guest · read-only',
            'none': 'Solo',
            'unknown': 'Checking session…',
        }[controller.role] if session else 'Game disconnected')
        connected_session = controller.connected_session()
        connected = connected_session is not None
        self.refresh_search_progress(connected_session)
        message = controller.message if connected else 'Connecting to Reborn…' if session else 'Start Reborn to find stats.'
        self.summary.set(message)
        self.find_button.configure(text='Stop search' if policy.searching and connected else 'Find',
                                   state='normal' if connected else 'disabled')
        self.max_all.configure(state='normal' if connected else 'disabled')
        self.pause.configure(text='Resume freezes' if policy.paused else 'Pause freezes',
                             state='normal' if policy.paused or policy.holds else 'disabled')

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
            if read_only and known:
                state = 'Read-only'
            elif policy.paused and (frozen or key in policy.pending):
                state = 'Paused'
            elif key in freeze.get('errors', {}):
                state = 'Retrying' if known else 'Waiting'
            elif key in policy.pending:
                state = 'Queued'
            elif known:
                state = 'Ready'
                if frozen:
                    state = 'Frozen' if key in freeze.get('confirmed', {}) else 'Verifying'
            values = (view.label, view.display(policy.values.get(key)), target, 'On' if frozen else 'Off', state)
            if tuple(self.table.item(key, 'values')) != values:
                self.table.item(key, values=values)

        view = BY_KEY[self.selected]
        if not self.freeze_edited:
            self.editor_frozen.set(view.key in policy.holds)
        self.title.set(view.label)
        self.live.set(view.display(policy.values.get(view.key)))
        self.target_label.set('Target' + (f' ({view.unit})' if view.unit else ''))
        self.entry.configure(state='disabled' if view.key == 'health' else 'normal')
        self.help.set(freeze.get('errors', {}).get(view.key) or view.note
                      or f'Range {view.minimum:,}–{view.maximum:,}{view.unit}')
        can_apply = view.key in policy.verified and not (policy.paused or read_only)
        self.apply_button.configure(text='Apply' if can_apply else 'Queue target')

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

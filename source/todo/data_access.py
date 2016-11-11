import sqlite3, json, os
import os.path as op
from datetime import datetime

from . import utils


DATA_DIR_NAME = '.toduh'
DATAFILE_NAME = 'data.json'
DATABASE_NAME = 'data.sqlite'
DATA_CTX_NAME = 'contexts'

# If a .toduh exists in the current working directory, it's used by the
# program. Otherwise the one in the home is used.
if op.exists(DATA_DIR_NAME) and op.isdir(DATA_DIR_NAME):
	DATA_DIR = DATA_DIR_NAME
else:
	DATA_DIR = op.expanduser(op.join('~', '.toduh'))

DB_PATH = op.join(DATA_DIR, DATABASE_NAME)


def setup_data_access():
	""" Prepare the sqlite database so that it's ready to be used by the
	application. It's supposed to work in any environment (new installation,
	old version, etc) and will make any necessary conversion between different
	versions (for example converting the json datafile from v2.2- into a
	sqlite database)"""
	if not op.exists(DATA_DIR):
		os.makedirs(DATA_DIR)
	# Three possibilities:
	#  - We find the sqlite database (v3+). Nothing to do
	#  - We find the JSON file (v2.2-). We need to set the database up and
	#    transfer the data from JSON to the database
	#  - We find nothing. We need to set the database up.
	json_path = op.join(DATA_DIR, DATAFILE_NAME)
	if op.exists(DB_PATH):
		pass
	elif op.exists(json_path):
		connection = setup_database(DB_PATH)
		with open(json_path) as datafile:
			data = json.load(datafile)
		transfer_data(connection, data)
	else:
		setup_database(DB_PATH).close()
	return DB_PATH


def setup_database(db_path):
	""" Creates the sqlite database and creates its tables, indexes, etc."""
	from .init_db import INIT_DB
	conn = sqlite3.connect(db_path)
	c = conn.cursor()
	for stmt in INIT_DB:
		c.execute(stmt)
	conn.commit()
	return conn


def transfer_data(connection, data):
	""" Transfer all data from a v2.2- JSON datafile held in the `data`
	dictionary into a sqlite database connected to with `connection`."""
	daccess = DataAccess(connection)
	for ctx, props in data['contexts'].items():
		ctx = dbfy_context(ctx)
		options = []
		if 'p' in props:
			options.append(('priority', props['p']))
		if 'v' in props and props['v'] == 'hidden':
			options.append(('visibility', 'hidden'))
		daccess.get_or_create_context(ctx, options)
	for task in data['tasks']:
		title = task['content']
		if 'context' in task:
			ctx = dbfy_context(task['context'])
			daccess.get_or_create_context(ctx)
		else:
			ctx = ''

		options = []
		if 'created' in task:
			created = iso2sqlite(task['created'])
		else:
			created = datetime.min.strftime(utils.SQLITE_DT_FORMAT)
		options.append(('created', created))
		for prop in ['start', 'deadline']:
			if prop in task:
				options.append((prop, iso2sqlite(task[prop])))
		if 'done' in task and task['done']:
			options.append(('done', '1'))
		if 'priority' in task:
			options.append(('priority', task['priority']))
		daccess.add_task(title, ctx, options)
	daccess.exit()


# In the database, contexts all descent from the root context ('') and any
# non-root context therefore starts with a dot as the dot separates
# different hierarchical levels (<empty string> <dot> <subcontext name>).
# For convenience, we allow the user to type a context's path without the
# starting dot. To do this, we manually add a dot at the begining of path
# that doesn't already start with a dot, unless the path is empty in which
# case it explicitly designates the root context.
#
# dbfy_context and userify_context perform necessary convertions between user-
# typed contexts' path and their DB representation.

def dbfy_context(ctx):
	if ctx == '' or ctx.startswith('.'):
		return ctx
	else:
		return '.'+ctx


def userify_context(ctx):
	if ctx == '':
		return ''
	else:
		return ctx[1:]

# This one is used by transfer_data
def iso2sqlite(iso_date):
	dt = datetime.strptime(iso_date, utils.ISO_DATE)
	return dt.strftime(utils.SQLITE_DT_FORMAT)


def get_insert_components(options):
	""" Takes a list of 2-tuple in the form (option, value) and returns a
	triplet (colnames, placeholders, values) that permits making a database
	query as follows: c.execute('INSERT INTO Table ({colnames}) VALUES
	{placeholders}', values). """
	col_names = ','.join(opt[0] for opt in options)
	placeholders = ','.join('?' for i in range(len(options)))
	if len(col_names) > 0:
		col_names = ',' + col_names
	if len(placeholders) > 0:
		placeholders = ',' + placeholders
	values = tuple(opt[1] for opt in options)
	return col_names, placeholders, values


def get_update_components(options):
	""" Same as get_insert_components but for update queries. Returns a tuple
	in the form (placeholders, values) to be used as follows:
	c.execute('UPDATE Table SET {placeholders}', values)"""
	placeholders = ','.join(
		'{}=?'.format(opt[0])
		for opt in options
	)
	values = tuple(opt[1] for opt in options)
	return placeholders, values


def check_options(options, allowed_options):
	for option, val in options:
		if option not in allowed_options:
			raise ValueError('Illegal option')


def rename_context(path, name):
	""" Returns a string which is what the path of the context would be if the
	context pointed to by `path` would be renamed with `name`."""
	return '.'.join(path.split('.')[:-1]+[name])


TASK_OPTIONS = {
	'title',
	'deadline',
	'start',
	'priority',
	'context'
}

CONTEXT_OPTIONS = {
	'priority',
	'visibility'
}


class DataAccess():

	""" Wrap SQL operations into an methods-based interface. An instance of
	DataAccess is created thanks to a DB-API connection object that is
	connected to a sqlite database setup for todo.

	Some methods return instances of Row objects from the database. Such Row
	objects support the mapping protocol. More specifically, when the
	documentation mention Row-task objects, it's a mapping which represent a
	task with the following keys: id, title, created, deadline, start,
	priority, done, context, ctx_path where context is the context ID the task
	belongs to and ctx_path is the path of the same context.

	Row-context objects represent a context with the following keys: id, path,
	priority, visibility, own_tasks, total_tasks where own_tasks is the number
	of tasks which directly belong to the context and total_tasks is the
	number of tasks which belong to the context or one of the contexts in its
	descendance.

	The following list sums up the types and formats that must be used when
	calling methods and that are used in the returned values from the methods:

	 * Task ID are integers (type int). The hexadecimal representation is
	   dealt with by the calling application.

	 * Context path are fully dotted (non-root contexts start with a dot since
	   it's <empty string (name of the root context)> <dot> <name of the
	   subcontext>). The starting dot is removed by the calling application
	   for user convenience.

	 * Datetimes are strings in the %Y-%m-%d %H:%M:%S format (see Python
	   datetimes formatting reference)

	When a method accepts an `options` argument, it awaits a list of 2-tuple
	in the form (column name, value).
	"""

	def __init__(self, connection):
		self.connection = connection
		c = self.connection.cursor()
		c.execute('PRAGMA case_sensitive_like = ON;')
		c.execute('PRAGMA foreign_keys = ON;')
		c.execute('PRAGMA synchronous = OFF;')
		self.connection.row_factory = sqlite3.Row
		self.changed_contexts = False

	def add_task(self, title, context='', options=[]):
		""" Add a task titled `title` and associated to the given `context`,
		with the given `options`. The context is created if not already
		existing."""
		check_options(options, TASK_OPTIONS)
		cid = self.get_or_create_context(context)
		query_tmp = """
			INSERT INTO Task (title, context {})
			VALUES (?, ? {})
		"""
		col_names, placeholders, values = get_insert_components(options)
		values = (title, cid) + values
		query = query_tmp.format(col_names, placeholders)

		c = self.connection.cursor()
		c.execute(query, values)
		return c.lastrowid

	def update_task(self, tid, context=None, options=[]):
		""" Update the task identified by ID `tid` (int) with the given
		`options`. If the context of the task needs to be updated as well,
		then `context` should be passed the dotted path of the new context.
		Otherwise it should be None."""
		check_options(options, TASK_OPTIONS)
		if context is not None:
			cid = self.get_or_create_context(context)
			options = options.copy()
			options.append(('context', cid))
		query_tmp = """
			UPDATE Task SET {}
			WHERE id = ?
		"""
		placeholders, values = get_update_components(options)
		values += (tid,)
		query = query_tmp.format(placeholders)

		c = self.connection.cursor()
		c.execute(query, values)
		return c.rowcount

	def get_task(self, tid, columns='*'):
		""" Get the task identified by ID `tid` (int). Return a Row object
		which supports the mapping protocol with database column names as
		keys. A subset of the columns can be retrieved by changing the value
		of the `columns` argument which must contain a comma-separated list of
		columns (a string)."""
		query = """
			SELECT {}
			FROM Task
			WHERE id = ?
		""".format(columns)

		c = self.connection.cursor()
		c.execute(query, (tid,))
		row = c.fetchone()
		if row is None:
			return None
		return row

	def do_many(self, function, tids):
		""" Call the method `function` for each task ID in the `tids` list.
		`function` should accept only one positional argument (in addition to
		self) which is a task ID."""
		missing = []
		for tid in tids:
			updated = getattr(self, function)(tid)
			if updated == 0:
				missing.append(tid)
		return missing

	def set_done_many(self, tids):
		return self.do_many('set_done', tids)

	def remove_many(self, tids):
		return self.do_many('remove', tids)

	def set_done(self, tid):
		c = self.connection.cursor()
		c.execute("""
			UPDATE Task SET done = datetime('now')
			WHERE id = ?
			AND done IS NULL
		""", (tid,))
		return c.rowcount

	def remove(self, tid):
		c = self.connection.cursor()
		c.execute("""
			DELETE FROM Task
			WHERE id = ?
		""", (tid,))
		return c.rowcount

	def get_or_create_context(self, path, options=[]):
		""" Get the context whose path is `path`. If the context
		doesn't exists, it is created as well as well all necessary
		intermediary contexts. If the context is created, then `options` are
		applied to the newly created context.

		Return the ID of the context."""
		check_options(options, CONTEXT_OPTIONS)
		ctxs = path.split('.')[1:]
		path_so_far = ''
		c = self.connection.cursor()
		for ctx in ctxs:
			path_so_far += '.'+ctx
			try:
				c.execute("""
					INSERT INTO Context (path)
					VALUES (?)
				""", (path_so_far,))
			except sqlite3.IntegrityError: # Already exists
				continue
			else:
				self.changed_contexts = True

		query_tmp = """
			INSERT INTO Context (path {})
			VALUES (? {})
		"""
		col_names, placeholders, values = get_insert_components(options)
		query = query_tmp.format(col_names, placeholders)

		try:
			c.execute(query, (path,) + values)
		except sqlite3.IntegrityError as e:
			c2 = self.connection.cursor()
			c2.execute("""
				SELECT id FROM Context
				WHERE path = ?
			""", (path,))
			row = c2.fetchone()
			return row[0]
		else:
			self.changed_contexts = True
			return c.lastrowid

	def context_exists(self, path):
		""" Return a boolean indicating whether the context pointed to by the
		dotted path `path` exists."""
		c = self.connection.cursor()
		c.execute("""
			SELECT 1 FROM Context
			WHERE path = ?
		""", (path,))
		row = c.fetchone()
		return row is not None
		
	def set_context(self, path, options=[]):
		""" Set the context pointed to by `path` to have the given `options`.
		If the context doesn't already exist, it's created, which is why this
		method is called set_context and not update_context.

		Return the number of rows affected."""
		check_options(options, CONTEXT_OPTIONS)
		cid = self.get_or_create_context(path)
		query_tmp = """
			UPDATE Context SET {}
			WHERE id = ?
		"""
		placeholders, values = get_update_components(options)
		query = query_tmp.format(placeholders)

		c = self.connection.cursor()
		c.execute(query, values + (cid,))
		return c.rowcount

	def move(self, ctx1, ctx2):
		""" Move all the direct tasks from ctx1 to ctx2 (dotted paths).
		Doesn't affect subcontexts (and subtasks) of ctx1."""
		cid = self.get_or_create_context(ctx2)
		c = self.connection.cursor()
		c.execute("""
			UPDATE Task
			SET context = ?
			WHERE context = (
				SELECT id FROM Context
				WHERE path = ?
			)
		""", (cid, ctx1))

	def remove_context(self, path):
		""" Remove the context (and all subcontexts and tasks/subtasks)
		pointed to by `path`."""
		c = self.connection.cursor()
		c.execute("""
			DELETE FROM Context
			WHERE path LIKE ?
		""", ('{}%'.format(path),))
		self.changed_contexts = True
		return c.rowcount

	def rename_context(self, path, name):
		"""Rename context with given path with name. Returns None if new name
		already exists, number of row affected otherwise. `name` must NOT contain a dot.
		"""
		assert '.' not in name
		renamed = rename_context(path, name)
		# Lock the db at the first select to avoid race conditions
		self.connection.isolation_level = 'IMMEDIATE'
		# If the context (after renaming) already exists, abort
		c = self.connection.cursor()
		c.execute("""
			SELECT 1 FROM Context WHERE path = ?
		""", (renamed,))
		if c.fetchone() is not None:
			return None
		# We need to rename all the subcontexts as well. Can't do it in one
		# UPDATE query because sqlite's REPLACE would replace all occurrences
		# and we risk renaming other things (contexts whose same structure
		# repeats at a sub-level)
		c = self.connection.cursor()
		c.execute("""
			SELECT id, path FROM Context WHERE path LIKE ?
		""", ('{}%'.format(path),))
		new = ((renamed + row['path'][len(path):], row['id']) for row in c)
		c2 = self.connection.cursor()
		c2.executemany("""
			UPDATE Context
			SET path = ?
			WHERE ID = ?
		""", new)
		return c2.rowcount

	def todo(self, path='', recursive=False):
		""" Return a list of Row-tasks which belong the the context pointed to
		by `path`. If `recursive` is False, then the list only contains tasks
		that *directly* belong to the context. Otherwise it contains tasks
		from descendance as well. In the list, tasks are sorted by:
		  * priority, descending
		  * remaining time (before deadline, infinity if no deadline),
		    ascending
		  * datetime created, ascending
		"""
		if recursive:
			operator, value = 'LIKE', '{}%'.format(path)
		else:
			operator, value = '=', path
		c = self.connection.cursor()
		c.execute("""
			SELECT t.*, c.path as ctx_path
			FROM Task t JOIN Context c
			ON t.context = c.id
			WHERE c.path {} ?
			  AND t.done IS NULL
			  AND (datetime('now')) >= datetime(t.start)
			ORDER BY
			  priority DESC,
			  COALESCE(
			      julianday(deadline),
			      julianday('9999-12-31 23:59:59')
			    ) - julianday('now') ASC,
			  created ASC
		""".format(operator), (value,))
		return c.fetchall()

	def get_subcontexts(self, path=''):
		""" Return a list of Row-contexts that are direct children of the
		context pointed to by `path`. The list doesn't contain contexts that
		have a "hidden" visibility. In the list, contexts are sorted by:
		 * priority, descending
		 * total number of tasks (including tasks in descendance), ascending
		"""
		c = self.connection.cursor()
		c.execute("""
			SELECT c.*, COUNT(own.id) as own_tasks, (
				SELECT COUNT(t.id)
				FROM Context c1
				LEFT JOIN Task t
				  ON t.context = c1.id
				 AND t.start <= (datetime('now'))
				 AND t.done IS NULL
				WHERE c1.path LIKE c.path||'%' 
			) as total_tasks
			FROM Context c
			LEFT JOIN Task own
			  ON own.context = c.id
			 AND own.start <= (datetime('now'))
			 AND own.done IS NULL
			WHERE path LIKE ?
			  AND path NOT LIKE ?
			  AND visibility = 'normal'
			GROUP BY c.id
			ORDER BY
			  priority DESC,
			  total_tasks DESC
		""", (
			'{}.%'.format(path),
			'{}.%.%'.format(path)
			)
		)
		return c.fetchall()

	def get_descendants(self, path=''):
		""" Return an iterator over Row-contexts that are in the descendance
		of the context pointed to by `path`. The iterator is agnostic of
		visibility and the contexts are sorted by their path. The first
		element of the iterator is the given context itself. """
		c = self.connection.cursor()
		c.execute("""
			SELECT c.*, COUNT(own.id) as own_tasks, (
				SELECT COUNT(t.id)
				FROM Context c1
				LEFT JOIN Task t
				  ON t.context = c1.id
				 AND t.start <= (datetime('now'))
				 AND t.done IS NULL
				WHERE c1.path LIKE c.path||'%' 
			) as total_tasks
			FROM Context c
			LEFT JOIN Task own
			  ON own.context = c.id
			 AND own.start <= (datetime('now'))
			 AND own.done IS NULL
			WHERE path LIKE ?
			GROUP BY c.id
			ORDER BY
			  c.path
		""", ('{}%'.format(path),))
		return c

	def history(self):
		""" Return an iterator over Row-tasks which iterates over all the
		tasks in existence, sorted by their date of creation."""
		c = self.connection.cursor()
		c.execute("""
			SELECT t.*, c.path as ctx_path
			FROM Task t JOIN Context c
			ON t.context = c.id
			ORDER BY t.created
		""")
		return c

	def get_greatest_id(self):
		""" Returns the greatest existing task ID, or None if there are no
		task."""
		c = self.connection.cursor()
		c.execute("""
			SELECT MAX(id)
			FROM Task
		""")
		row = c.fetchone()
		if row is None:
			return None
		else:
			return row[0]

	def purge(self, before):
		""" Remove all done tasks that were created before `before`. Remove
		all done tasks if `before` is None."""
		c = self.connection.cursor()
		query = """
			DELETE FROM Task
			WHERE done IS NOT NULL
		"""
		if before is not None:
			query += """
				AND created < ?
			"""
			values = (before,)
		else:
			values = ()
		c.execute(query, values)
		return c.rowcount

	def exit(self, save=True):
		""" Close the database and save all operations done to it if `save` is
		True. Write all contexts paths (NON fully-dotted) to the contexts file
		if at least one context was created or removed during operations. The
		contexts file exists for terminal auto-completion."""
		if save:
			self.connection.commit()
			if self.changed_contexts:
				c = self.connection.cursor()
				c.execute("""
					SELECT DISTINCT path FROM Context
					ORDER BY path
				""")
				data_ctx = op.join(DATA_DIR, DATA_CTX_NAME)
				with open(data_ctx, 'w') as ctx_file:
					for row in c:
						ctx = userify_context(row[0])
						ctx_file.write(ctx + '\n')
		self.connection.close()

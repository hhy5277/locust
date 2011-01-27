import locust
import core
from core import Locust, hatch, MasterLocustRunner, SlaveLocustRunner, LocalLocustRunner
from stats import print_stats
import web

import gevent
import sys
import os
import inspect
from optparse import OptionParser, make_option

_internals = []

env_options = [
    make_option('-H', '--hosts',
                default=[],
                help="comma-separated list of hosts to operate on"
                ),

    make_option('-f', '--locustfile',
                default='locust',
                help="Python module file to import, e.g. '../other.py'"
                ),
]

def parse_options():
	"""
	Handle command-line options with optparse.OptionParser.

	Return list of arguments, largely for use in `parse_arguments`.
	"""

	# Initialize
	parser = OptionParser(usage="locust [options] <command>[:arg1,arg2=val2,host=foo,hosts='h1;h2',...] ...")

	# Version number (optparse gives you --version but we have to do it
	# ourselves to get -V too. sigh)
	parser.add_option(
	    '-V', '--version',
	    action='store_true',
	    dest='show_version',
	    default=False,
	    help="show program's version number and exit"
	)

	# List locust commands found in loaded locust files/source files
	parser.add_option(
	    '-l', '--list',
	    action='store_true',
	    dest='list_commands',
	    default=False,
	    help="print list of possible commands and exit"
	)

	# Like --list, but text processing friendly
	parser.add_option(
	    '--shortlist',
	    action='store_true',
	    dest='shortlist',
	    default=False,
	    help="print non-verbose list of possible commands and exit"
	)
	
	# if we shgould print stats in the console
	parser.add_option(
	    '--print-stats',
	    action='store_true',
	    dest='print_stats',
	    default=False,
	    help="Print stats in the console"
	)
	
	# if we should print stats in the console
	parser.add_option(
	    '--no-web',
	    action='store_true',
	    dest='no_web',
	    default=False,
	    help="Disable the web monitor"
	)
	
	# if locust should be run in distributed mode as master
	parser.add_option(
	    '--master',
	    action='store_true',
	    dest='master',
	    default=False,
	    help="Set locust to run in distributed mode with this process as master"
	)
	
	# if locust should be run in distributed mode as slave
	parser.add_option(
	    '--slave',
	    action='store_true',
	    dest='slave',
	    default=False,
	    help="Set locust to run in distributed mode with this process as slave"
	)
	
	# Number of clients
	parser.add_option(
	    '-c', '--clients',
	    action='store',
	    type='int',
	    dest='num_clients',
	    default=1,
	    help="Number of concurrent clients"
	)
	
	# Client hatch rate
	parser.add_option(
	    '-r', '--hatch-rate',
	    action='store',
	    type='int',
	    dest='hatch_rate',
	    default=1,
	    help="The rate per second in which clients are spawned"
	)
	
	# redis options
	parser.add_option(
	    '--redis-host',
	    action='store',
	    type='str',
	    dest='redis_host',
	    default="localhost",
	    help="Redis host to use for distributed load testing"
	)
	parser.add_option(
	    '--redis-port',
	    action='store',
	    type='int',
	    dest='redis_port',
	    default=6379,
	    help="Redis port to use for distributed load testing"
	)

	# Add in options which are also destined to show up as `env` vars.
	for option in env_options:
		parser.add_option(option)

	# Finalize
	# Return three-tuple of parser + the output from parse_args (opt obj, args)
	opts, args = parser.parse_args()
	return parser, opts, args


def _is_package(path):
	"""
	Is the given path a Python package?
	"""
	return (
		os.path.isdir(path)
		and os.path.exists(os.path.join(path, '__init__.py'))
	)


def find_locustfile(locustfile):
	"""
	Attempt to locate a locustfile, either explicitly or by searching parent dirs.
	"""
	# Obtain env value
	names = [locustfile]
	# Create .py version if necessary
	if not names[0].endswith('.py'):
		names += [names[0] + '.py']
	# Does the name contain path elements?
	if os.path.dirname(names[0]):
		# If so, expand home-directory markers and test for existence
		for name in names:
			expanded = os.path.expanduser(name)
			if os.path.exists(expanded):
				if name.endswith('.py') or _is_package(expanded):
					return os.path.abspath(expanded)
	else:
		# Otherwise, start in cwd and work downwards towards filesystem root
		path = '.'
		# Stop before falling off root of filesystem (should be platform
		# agnostic)
		while os.path.split(os.path.abspath(path))[1]:
			for name in names:
				joined = os.path.join(path, name)
				if os.path.exists(joined):
					if name.endswith('.py') or _is_package(joined):
						return os.path.abspath(joined)
			path = os.path.join('..', path)
	# Implicit 'return None' if nothing was found


def is_locust(tup):
	"""
	Takes (name, object) tuple, returns True if it's a public Locust subclass.
	"""
	name, item = tup
	return (
		inspect.isclass(item)
		and issubclass(item, Locust)
		and (item not in _internals)
		and not name.startswith('_')
	)


def load_locustfile(path):
	"""
	Import given locustfile path and return (docstring, callables).

	Specifically, the locustfile's ``__doc__`` attribute (a string) and a
	dictionary of ``{'name': callable}`` containing all callables which pass
	the "is a Locust" test.
	"""
	# Get directory and locustfile name
	directory, locustfile = os.path.split(path)
	# If the directory isn't in the PYTHONPATH, add it so our import will work
	added_to_path = False
	index = None
	if directory not in sys.path:
		sys.path.insert(0, directory)
		added_to_path = True
	# If the directory IS in the PYTHONPATH, move it to the front temporarily,
	# otherwise other locustfiles -- like Locusts's own -- may scoop the intended
	# one.
	else:
		i = sys.path.index(directory)
		if i != 0:
			# Store index for later restoration
			index = i
			# Add to front, then remove from original position
			sys.path.insert(0, directory)
			del sys.path[i + 1]
	# Perform the import (trimming off the .py)
	imported = __import__(os.path.splitext(locustfile)[0])
	# Remove directory from path if we added it ourselves (just to be neat)
	if added_to_path:
		del sys.path[0]
	# Put back in original index if we moved it
	if index is not None:
		sys.path.insert(index + 1, directory)
		del sys.path[0]
	# Return our two-tuple
	locusts = dict(filter(is_locust, vars(imported).items()))
	return imported.__doc__, locusts

def main():
	print ""
	parser, options, arguments = parse_options()
	#print "Options:", options, dir(options)
	#print "Arguments:", arguments
	#print "largs:", parser.largs
	#print "rargs:", parser.rargs

	if options.show_version:
		print("Locust %s" % ("0.1"))
		sys.exit(0)

	locustfile = find_locustfile(options.locustfile)
	if not locustfile:
		print "Could not find any locustfile!"
		sys.exit(1)

	docstring, locusts = load_locustfile(locustfile)
	
	if options.list_commands:
		print "Available Locusts:"
		for name in locusts:
			print "    " + name
		sys.exit(0)
	
	# make sure specified Locust exists
	if not arguments[0] in locusts.keys():
		sys.stderr.write("Unknown Locust: %s\n" % (arguments[0]))
		sys.exit(1)
	else:
		locust_class = locusts[arguments[0]]
	
	if not options.no_web and not options.slave:
		# spawn web greenlet
		gevent.spawn(web.start, locust_class, options.hatch_rate, options.num_clients)
	
	if not options.master and not options.slave:
		core.locust_runner = LocalLocustRunner(locust_class, options.hatch_rate, options.num_clients)
		if options.no_web:
			# spawn client spawning/hatching greenlet
			core.locust_runner.start_hatching()
		
		if options.print_stats or options.no_web:
			# spawn stats printing greenlet
			gevent.spawn(print_stats)
	elif options.master:
		core.locust_runner = MasterLocustRunner(locust_class, options.hatch_rate, options.num_clients, redis_host=options.redis_host, redis_port=options.redis_port)
	elif options.slave:
		core.locust_runner = SlaveLocustRunner(locust_class, options.hatch_rate, options.num_clients, redis_host=options.redis_host, redis_port=options.redis_port)
	
	try:
		gevent.sleep(100000)
	except KeyboardInterrupt, e:
		print ""
		print "Exiting, bye.."
		print ""
	
	sys.exit(0)
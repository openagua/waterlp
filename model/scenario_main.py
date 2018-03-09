import sys
import traceback
from io import StringIO
from os.path import join
import json

from model import create_model
from pyomo.opt import SolverFactory, SolverStatus, TerminationCondition
from logger import create_logger

from post_reporter import Reporter as PostReporter
from ably_reporter import AblyReporter

current_step = 0
total_steps = 0    
        
def run_scenario(supersubscenario, conn=None, args=None):
    
    global current_step, total_steps
    
    system = supersubscenario.get('system')
    
    # setup the reporter (ably is on a per-process basis)
    post_reporter = PostReporter(args)
    if args.message_protocol is None:
        reporter = None
    elif args.message_protocol == 'post':
        post_reporter.is_main_reporter = True
        reporter=post_reporter
    elif args.message_protocol == 'ably': # i.e. www.ably.io
        ably_reporter = AblyReporter(args, post_reporter=post_reporter)
        reporter=ably_reporter    
    
    if reporter:
        reporter.updater = system.scenario.update_payload
        system.scenario.reporter = reporter
        
    if post_reporter:
        post_reporter.updater = system.scenario.update_payload
    
    try:
        
        for result in _run_scenario(system, args, conn, supersubscenario, reporter=reporter):
            pass            
           
    except Exception as e:

        #print(e, file=sys.stderr)
        # Exception logging inspired by: https://seasonofcode.com/posts/python-multiprocessing-and-exceptions.html
        exc_buffer = StringIO()
        traceback.print_exc(file=exc_buffer)
        msg = 'At step ' + str(current_step) + ' of ' + str(total_steps) + ': ' + \
              str(e) + '\nUncaught exception in worker process:\n' + exc_buffer.getvalue()
        if current_step:
            msg += '\n\nPartial results have been saved'
            
        print(msg)
            
        if reporter:
            reporter.report(action='error', message=msg)

def _run_scenario(system=None, args=None, conn=None, supersubscenario=None, reporter=None):
    global current_step, total_steps
    
    debug = args.debug
            
    # initialize with scenario
    #current_dates = system.dates[0:foresight_periods]    
    
    # intialize
    system.prepare_params()
    
    # set up variations
    variation_sets = supersubscenario.get('variation_sets')
    system.setup_variations(variation_sets)
    
    # initialize boundary conditions
    system.update_boundary_conditions(0, system.foresight_periods, initialize=True)
    
    system.init_pyomo_params()
    system.model = create_model(
        name=system.name,
        template=system.template,
        nodes=list(system.nodes.keys()),
        links=list(system.links.keys()),
        types=system.ttypes,
        ts_idx=system.ts_idx,
        params=system.params,
        blocks=system.blocks,
        debug=debug
    )
    
    system.instance = system.model.create_instance()

    system.update_internal_params()

    optimizer = SolverFactory(args.solver)
    #logd.info('Model created.')
    #logd.info('Starting model run.')

    total_steps = len(system.dates)

    failed = False

    runs = range(system.nruns)
    
    if args.debug:
        runs = runs[:5]

    #last_year = arrow.get(system.timesteps[0]).year

    i = 0
    while i < len(runs):
    #for i, ts in enumerate(runs):
        
        ts = runs[i]
        current_step = i + 1       

        # if user requested to stop
        #if reporter._is_canceled:
            #print('canceled')
            #break
            
        #######################
        # CORE SCENARIO ROUTINE
        #######################

        current_dates = system.dates[ts:ts+system.foresight_periods]
        current_dates_as_string = system.dates_as_string[ts:ts+system.foresight_periods]

        # solve the model
        results = optimizer.solve(system.instance)
        #system.instance.solutions.load_from(results)
            
        # print & save summary results
        #if verbose:
            #old_stdout = sys.stdout
            #sys.stdout = summary = StringIO()
            #logd.info('model solved\n' + summary.getvalue())

        if (results.solver.status == SolverStatus.ok) \
           and (results.solver.termination_condition == TerminationCondition.optimal):
            # this is feasible and optimal
            #if verbose:
                #logd.info('Optimal feasible solution found.')

            system.collect_results(current_dates_as_string, write_input=args.write_input)

            #if verbose:
                #logd.info('Results saved.')

        elif results.solver.termination_condition == TerminationCondition.infeasible:
            system.save_results()
            msg = 'ERROR: Problem is infeasible at step {} of {} ({}). Prior results have been saved.'.format(
                current_step, total_steps, current_dates[0]
            )
            if system.scenario.reporter: 
                system.scenario.reporter.report(action='error', message=msg)
            break

        else:
            system.save_results()
            # something else is wrong
            msg = 'ERROR: Something went wrong. Likely the model was not built correctly.'
            print(msg)
            #logd.info(msg)
            payload = system.scenario.update_payload(action='error', payload={'message': msg})
            if system.scenario.reporter:
                system.scenario.reporter.report(action='error', message=msg)
            break

        #if foresight_periods == 1:
        #print("Writing results...")
        #results.write()

        #else:

        # load the results
        #print("Loading results...")
        system.instance.solutions.load_from(results)
        #if verbose:
            #sys.stdout = old_stdout
            
        system.scenario.finished += 1
            
        if system.scenario.reporter:
            system.scenario.reporter.report(action='step')

        # update the model instance
        if ts != runs[-1]:
            ts_next = runs[i+1]
            system.update_initial_conditions()
            system.update_boundary_conditions(ts, ts+system.foresight_periods) # SLOW
            system.update_internal_params() # update internal parameters that depend on user-defined variables
            system.instance.preprocess()

        else:
            system.save_results()
            reporter and reporter.report(action='done')
            #logd.info('done run')

        #if verbose:
            #logd.info(
                #'completed timestep {date} | {timestep}/{total_timesteps}'.format(
                    #date=system.dates[ts],
                    #timestep=ts+1,
                    #total_timesteps=nruns
                #)
            #)

        #######################
        
        i += 1
        
        yield

    # POSTPROCESSING HERE (IF ANY)

    #reporter.done(current_step, total_steps)
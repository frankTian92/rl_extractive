import sys; print('Python %s on %s\n' % (sys.version, sys.platform))
import os
sys.path.extend([os.getcwd(), '/'.join(os.getcwd().split('/')[:-1])])
print(sys.path)

import argparse
import numpy as np
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from itertools import product
from rlex.rl_extraction import PolicyGradientExtractor
from rlex.oracle_extraction import RandomSummarizer, GreedyOracleSummarizer, Lead3Summarizer
from rlex.abstract_extraction import Params
from rlex.load_data import get_samples

NUM_TRAINING_ARTICLES = 200

FLOAT_PARAMS = {'v_lr', 'p_lr'}
FEATURES = {'pca_features', 'tfidf_max_features'}
PARAMS_TO_TEST = {
    'v_lr': [],
    'p_lr': [],
    'pca_features': [],
    'tfidf_max_features': [],
    'n_training_steps': [],
    'update_only_last': [True, False],
    'batch_mean': [True, False],
}

TINY_PARAMS_TEST = {
    'v_lr': [0],
    'p_lr': [0.275],
    'pca_features': [1000],
    'tfidf_max_features': [5000],
    'n_training_steps': [500],
    'update_only_last': [False],
    'batch_mean': [False],
}

BASELINE_PARAMS = {
    'v_lr': '-',
    'p_lr': '-',
    'pca_features': '-',
    'tfidf_max_features': '-',
    'n_training_steps': '-',
    'update_only_last': '-',
    'batch_mean': None, # put the name here, gross I know
}

class TaskLog(object):
    """
    Object to hold the results of a RL training task.
    Convenient for results serialization. Basically just a dict.
    """
    def __init__(self, params_tested, train_res, val_res, test_res, time_taken):
        self.task_conclusion = OrderedDict()
        for d in params_tested, train_res, val_res, test_res:
            for key, value in d.items():
                self.task_conclusion[key] = value
        self.task_conclusion['time_taken'] = '{:.4f}'.format(time_taken)

    def keys_to_csv_string(self):
        return ','.join(map(str, self.task_conclusion.keys()))

    def values_to_csv_string(self):
        return ','.join(map(str, self.task_conclusion.values()))


def iter_args_values(arg_namespace):
    """
    Convenient way to iterate over the args passed to an argparser.
    :param arg_namespace: the specific argparse instance
    :return: yielded argument with its value
    """
    for arg in arg_namespace.__dict__:
        if not arg.startswith('_'):
            yield arg, arg_namespace.__getattribute__(arg)

def set_params(arg_namespace, verbose=False):
    """
    Sets the global parameters we are testing over based on argv.
    :param arg_namespace: the argparse object
    :param verbose: Bool for printing more
    :return: None
    """
    global PARAMS_TO_TEST, TINY_PARAMS_TEST
    if verbose: print('\nSetting hyperparameters from argparse:')

    # for tiny specific testing
    if arg_namespace.tiny_test:
        PARAMS_TO_TEST = TINY_PARAMS_TEST
        arg_namespace.write_every = 1
        arg_namespace.verbose = True
        return

    # gridsearch setting
    for arg, value in iter_args_values(arg_namespace):
        hparam = arg.split('__range')[0]
        if hparam in PARAMS_TO_TEST and type(value) is tuple:
            PARAMS_TO_TEST[hparam] = list(np.linspace(value[0], value[1], value[2]))
            if hparam in FLOAT_PARAMS:
                PARAMS_TO_TEST[hparam] = list(map(lambda x: np.around(x, 5), PARAMS_TO_TEST[hparam]))
            else:
                PARAMS_TO_TEST[hparam] = list(map(int, PARAMS_TO_TEST[hparam]))
            if verbose:
                print(f'\t{hparam}: tests the following {len(PARAMS_TO_TEST[hparam])}'
                      f'values\n\t\t{PARAMS_TO_TEST[hparam]}')
        elif verbose:
            print(f'\t{hparam}: {value} -- not a hyperparameter, skipping.')

def generate_param_tests(component, n_components):
    """
    Generates all the hyperparameter configurations that we will
        be testing over, based on parallelized component settings.
    :param component: the specific parallelization component
    :param n_components: the total number of parallel processes
        we are distributing over
    :return: List of parameter dictionaries
    """
    global PARAMS_TO_TEST
    all_params = list(PARAMS_TO_TEST.values())
    all_combos = list(product(*all_params))
    tests_per_component = int(len(all_combos) / n_components)
    start, j = 0, 0
    while j < component:
        start += tests_per_component
        j += 1
    assert(start + tests_per_component <= len(all_combos))

    # build the test-param dictionaries
    all_tests = []
    keys = list(PARAMS_TO_TEST.keys())
    for combo in all_combos[start: start+tests_per_component]:
        all_tests.append({key: value for key, value in zip(keys, combo)})
    return all_tests

def run_rl_task(train_a, val_a, test_a, params_dict, verbose=False):
    """
    Runs a specific task for testing our RL model based on parameters passed.
    :param train_a: List of articles to train on
    :param val_a: articles for validation testing
    :param test_a: List of articles to test on
    :param params_dict: Dict of parameters we are testing for this task
    :param verbose: verbosity 101
    :return: TaskLog object storing our results and parameters for this task
    """
    start = time.time()
    params = Params(
        gamma=1,
        v_lr=params_dict['v_lr'],
        p_lr=params_dict['p_lr'],
        use_baseline=params_dict['v_lr'] != 0,
        update_only_last=params_dict['update_only_last'],
    )
    model = PolicyGradientExtractor(params)
    if verbose: print('\textracting features...')
    model.set_features(train_a + val_a + test_a, **{k: v for k, v in params_dict.items() if k in FEATURES})
    if verbose: print('\ttraining model...')
    model.train_on_batch_articles(
        article_training_steps=params_dict['n_training_steps'],
        articles=train_a,
        batch_mean=params_dict['batch_mean'],
        track_greedy=False,
        track_results=False,
        shuffle=False,
    )
    # now store the train and test results
    if verbose: print('\tgetting final results to report...')
    train_results = get_article_set_results(model, train_a, 'train')
    val_results = get_article_set_results(model, val_a, 'val')
    test_results = get_article_set_results(model, test_a, 'test')
    return TaskLog(params_dict, train_results, val_results, test_results, time.time() - start)

def run_baselines(train_a, val_a, test_a):
    task_logs = []
    for model in RandomSummarizer(1848), Lead3Summarizer(), GreedyOracleSummarizer():
        start = time.time()

        # a bit of a hack to work with the RL based parameter stuff
        baseline_params = deepcopy(BASELINE_PARAMS)
        baseline_params['batch_mean'] = model.name

        # get the results
        train_results = get_article_set_results(model, train_a, 'train')
        val_results = get_article_set_results(model, val_a, 'val')
        test_results = get_article_set_results(model, test_a, 'test')

        # make the task log
        tl = TaskLog(baseline_params, train_results, val_results, test_results, time.time() - start)
        task_logs.append(tl)
    return task_logs

def get_article_set_results(model, articles, test_name):
    """
    Gets the mean results over a set of articles across metrics.
    :param model: an Extractor object
    :param articles: List of Article
    :param test_name: String to indicate what we are testing over
    :return: Dict of results
    """
    scores = defaultdict(lambda: [])
    for a in articles:
        ex = model.extract_summary(a)
        for key, res in ex.rouge_res.items():
            scores[key].append(res['r'])
    return { f'{key}-{test_name}': np.mean(scores[key]) for key in scores }


if __name__ == '__main__':
    """
    The code below defines the arguments that can be passed to the program.
      E.g., when you call $ python gridsearch.py --ARGS (it's like argv in java)
    So then I can test different things without modifying the program, allows for modularity.
    The task is extractive summarization.
    
    Given an article, select 3 sentences to make the best possible summary of the article.
    
    I express this as a kind of wishy-washy RL problem. lol
    
    But the important feature of this software is generalization and abstraction (as with everything lol)
        that is, see the abstract_extraction.py file for the quite useful abstract "Extractor" class 
    
    The standard/traditional way to do extractive summarization uses heuristics.
        -> Turn all the sentences in the document into feature vectors
        -> Look at all of the sentences one by one, and see which one is most "different"
            (according to a distance function, e.g., cosine distance) and add that to the summary
        -> Redo the above till you have three sentences for the summary.
        -> Easy peesy lemon squeezy.
    
    RL says:
        -> The "state" is the article with its N sentences.
        -> An "action" is the choice of selecting 1 of the N sentences for the summary.
        -> The goal is to learn the best "policy" in a general way
            --> that is, we want the best sentences to be the ones we are most likely to select
            --> we need to learn P( a | s ) [probability that an action (sentence) belongs to the 
                summarization, given the current state (the document as a whole)
        -> RL == learning (specific algorithms are used for this) to act (e.g., select a sentence)  
            in an environment (e.g., the news article as a whole)
    """

    parser = argparse.ArgumentParser(description='Parallelized gridsearch over policy gradient parameters.')
    parser.add_argument('name', type=str,
                        help='global name for this gridsearch test')
    parser.add_argument('-c', '--component', type=int, default=0,
                        help='current parallized component index we want to use')
    parser.add_argument('-nc', '--n_components', type=int, default=1,
                        help='total number of parallel processes we will be doing')
    parser.add_argument('--n_samples', type=int, default=200,
                        help='number of samples we want to train on out of 250')
    parser.add_argument('-d', '--logdir', type=str, default='../results',
                        help='path to directory where we will serialize results')
    parser.add_argument('-f', '--force', action='store_true', default=False,
                        help='force overwrite of the current results directory')
    parser.add_argument('--v_lr__range', nargs=3, default=(0, 0.35, 15), type=tuple,
                        help='range of v_lr values to test, $3 total between $1 $2')
    parser.add_argument('--p_lr__range', nargs=3, default=(0.05, 0.35, 13), type=tuple,
                        help='range of p_lr values to test, $3 total between $1 $2')
    parser.add_argument('--pca_features__range', nargs=3, default=(100, 1000, 3), type=tuple,
                        help='range of pca_features values to test, $3 total between $1 $2')
    parser.add_argument('--tfidf_max_features__range', nargs=3, default=(2500, 5000, 2), type=tuple,
                        help='range of pca_features values to test, $3 total between $1 $2')
    parser.add_argument('--n_training_steps__range', nargs=3, default=(500, 500, 1), type=tuple,
                        help='range for number of training steps to test, $3 total between $1 $2')
    parser.add_argument('-v', '--verbose', default=False, action='store_true',
                        help='prints details for every model iterated over')
    parser.add_argument('--dry', default=False, action='store_true',
                        help='do a dry run, show all tasks to be performed without doing them')
    parser.add_argument('--tiny-test', default=False, action='store_true',
                        help='do a tiny test of 4 models to determine if this all works')
    parser.add_argument('-w', '--write-every', type=int, default=24,
                        help='number of tasks to perform before writing results to disk')
    args = parser.parse_args()

    print('\nArgparse parameters set to:')
    for arg_name, val in iter_args_values(args):
        if type(val) == tuple:
            val = tuple(map(float, val)) # turn strings to proper types
            args.__setattr__(arg_name, val)
        print('\t{}: {}'.format(arg_name, val))
    assert(args.component < args.n_components)

    # set the hyperparams for the GS with the argparse parameters
    set_params(args, verbose=args.verbose)
    tests = generate_param_tests(args.component, args.n_components)
    if args.verbose:
        print('\nGridsearch is a cartesian product over the following sets:')
        for param, test_vals in PARAMS_TO_TEST.items():
            print(f'\t{param}: {test_vals}')
        print(f'\nTesting the following {len(tests)} combinations:')
        for t in tests[:10]:
            print(f'\t{t}')
        if len(tests) > 10: print('\t...')

    # now we will set the directory structure
    path_to_results = f'{args.logdir}/{args.name}'
    try:
        os.mkdir(path_to_results)
    except FileExistsError:
        pass

    # get all the articles, clean always
    all_articles = get_samples(clean=True)
    train_articles = all_articles[:args.n_samples]
    val_articles = all_articles[args.n_samples:]
    test_articles = get_samples(clean=True, test=True)

    # run the tasks!
    crt_res_write = ''
    num_res = 0
    w = 0
    print('\nTask enumeration starting...')
    for i, param_test in enumerate(tests):
        if args.dry: continue
        print(f' <RLTask {i} - {param_test}>')

        # run the task, get results
        task_log = run_rl_task(train_articles, val_articles, test_articles, param_test, args.verbose)
        num_res += 1

        # write header (for first step), append the actrual values, add the baseline results too
        if i == 0:
            crt_res_write = '{}\n'.format(task_log.keys_to_csv_string())
            for baseline_tl in run_baselines(train_articles, val_articles, test_articles):
                crt_res_write = '{}{}\n'.format(crt_res_write, baseline_tl.values_to_csv_string())

        crt_res_write = '{}{}\n'.format(crt_res_write, task_log.values_to_csv_string())

        # serialize results into CSV
        if num_res >= args.write_every or i == len(tests) - 1:
            fname = '{}/{}_component{}-{}_iter-{}.csv'.format(
                path_to_results, args.name, args.component, args.n_components, w
            )
            print(f'\twriting to {fname}...')
            with open(fname, 'w') as f:
                f.write(crt_res_write)

            # put the header and reset
            crt_res_write = '{}\n'.format(task_log.keys_to_csv_string())
            num_res = 0
            w += 1
    print('\nTotal tasks: {}'.format(len(tests)))

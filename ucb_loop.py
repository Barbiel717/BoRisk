"""
This text is not updated!!!
This version is to be callable from some other python code.
A full optimization loop of VaRKG with some pre-specified parameters.
Specify the problem to use as the 'function', adjust the parameters and run.
Make sure that the problem is defined over unit-hypercube, including the w components.
The w components will be drawn as i.i.d. uniform(0, 1) and the problem is expected to convert these to appropriate
random variables.
"""
import torch
from torch import Tensor
from botorch.models import SingleTaskGP
from botorch.fit import fit_gpytorch_model
from gpytorch.mlls import ExactMarginalLogLikelihood
from VaR_UCB import InnerVaR, w_KG, pick_w_confidence
from time import time
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.constraints.constraints import GreaterThan
from gpytorch.priors.torch_priors import GammaPrior
from test_functions.function_picker import function_picker
from botorch.models.transforms import Standardize
from botorch.optim import optimize_acqf


def full_loop(function_name: str, seed: int, dim_w: int, filename: str, iterations: int,
              num_samples: int = 100, num_fantasies: int = 100,
              num_restarts: int = 100, raw_multiplier: int = 10,
              alpha: float = 0.7, q: int = 1,
              num_lookahead_repetitions: int = 0,
              lookahead_samples: Tensor = None, verbose: bool = False, maxiter: int = 100,
              CVaR: bool = False, expectation: bool = False,
              beta_c: float = 0, beta_d: float = 0,
              cuda: bool = False, reporting_la_samples: Tensor = None, reporting_la_rep: int = 0,
              random_sampling: bool = False, beta_pow: float = 2):
    """
    The full_loop in callable form
    :param seed: The seed for initializing things
    :param function_name: The problem function to be used.
    :param dim_w: Dimension of the w component.
    :param filename: Output file name.
    :param iterations: Number of iterations for the loop to run.
    :param num_samples: Number of samples of w to be used to evaluate C/VaR.
    :param num_fantasies: Number of fantasy models to construct in evaluating w_KG.
    :param num_restarts: Number of random restarts for optimization of VaRKG.
    :param raw_multiplier: Raw_samples = num_restarts * raw_multiplier
    :param alpha: The risk level of C/VaR.
    :param q: Number of parallel solutions to evaluate. Think qKG.
    :param num_lookahead_repetitions: Number of repetitions of lookahead fantasy evaluations.
    :param lookahead_samples: The samples to use to generate the lookahead fantasies
    :param verbose: Print more stuff and plot if d == 2.
    :param maxiter: (Maximum) number of iterations allowed for L-BFGS-B algorithm.
    :param CVaR: If true, use CVaR instead of VaR, i.e. CVaRKG.
    :param expectation: If true, we are running BQO optimization.
    :param beta_c: TODO: explain - these might have to go inside and become iteration dependent
    :param beta_d:
    :param cuda: True if using GPUs
    :param reporting_la_samples: lookahead samples for reporting of the best
    :param reporting_la_rep: lookahead replications for reporting of the best
    :param random_sampling: if True, samples are generated randomly
    :param beta_pow:
    :return:
    """

    # Initialize the test function
    function = function_picker(function_name)
    d = function.dim  # dimension of train_X
    n = 2 * d + 2  # training samples
    dim_x = d - dim_w  # dimension of the x component

    # fix the seed for testing - this only fixes the initial samples. The optimization still has randomness.
    torch.manual_seed(seed=seed)
    seed_list = torch.randint(1000000, (1000,))
    last_iteration = -1
    full_data = dict()
    train_X = torch.rand((n, d))
    train_Y = function(train_X, seed=seed_list[-1])

    # samples used to get the VaR value
    if dim_w == 1:
        w_samples = torch.linspace(0, 1, num_samples).reshape(num_samples, 1)
    else:
        w_samples = torch.rand((num_samples, dim_w))

    if verbose and d == 2:
        import matplotlib.pyplot as plt
        from plotter import contour_plotter
        plotter = contour_plotter

    # for timing
    start = time()

    inner_bounds = torch.tensor([[0.], [1.]]).repeat(1, dim_x)
    w_bounds = torch.tensor([[0.], [1.]]).repeat(1, dim_w)

    # a more involved prior to set a significant lower bound on the noise. Significantly speeds up computation.
    noise_prior = GammaPrior(1.1, 0.5)
    noise_prior_mode = (noise_prior.concentration - 1) / noise_prior.rate
    likelihood = GaussianLikelihood(
        noise_prior=noise_prior,
        batch_shape=[],
        noise_constraint=GreaterThan(
            0.05,  # minimum observation noise assumed in the GP model
            transform=None,
            initial_value=noise_prior_mode,
        ),
    )

    current_best_list = torch.empty((iterations + 1, q, dim_x))
    current_best_value_list = torch.empty((iterations + 1, q, 1))
    best_x_value_list = torch.empty((iterations, q, 1))
    candidate_list = torch.empty((iterations, q, d))

    # construct and fit the GP
    if cuda:
        gp = SingleTaskGP(train_X.cuda(), train_Y.cuda(), likelihood.cuda(), outcome_transform=Standardize(m=1)).cuda()
        mll = ExactMarginalLogLikelihood(gp.likelihood, gp).cuda()
        fit_gpytorch_model(mll).cuda()
    else:
        gp = SingleTaskGP(train_X, train_Y, likelihood, outcome_transform=Standardize(m=1))
        mll = ExactMarginalLogLikelihood(gp.likelihood, gp)
        fit_gpytorch_model(mll)

    passed = False  # it is a flag for handling exceptions
    handling_count = 0  # same
    i = last_iteration + 1

    while i < iterations:
        try:
            if not random_sampling:
                beta = beta_c * torch.log(torch.tensor([beta_d * (i + 1) ** beta_pow], dtype=torch.float))
                beta = float(beta)
            iteration_start = time()

            # similar to seed below, for the lookahead fantasies if used
            lookahead_seed = int(torch.randint(100000, (1,)))

            # seed for lookahead fantasies used for reporting.
            reporting_la_seed = int(torch.randint(100000, (1,)))

            inner_VaR = InnerVaR(model=gp, w_samples=w_samples, alpha=alpha, dim_x=dim_x,
                                 num_lookahead_repetitions=reporting_la_rep,
                                 lookahead_samples=reporting_la_samples,
                                 lookahead_seed=reporting_la_seed,
                                 CVaR=CVaR, expectation=expectation,
                                 beta=0, beta_max=0, cuda=cuda)

            current_best, current_best_value = optimize_acqf(acq_function=inner_VaR,
                                                             bounds=inner_bounds,
                                                             q=q,  # TODO: q>1 not implemented
                                                             num_restarts=num_restarts,
                                                             raw_samples=num_restarts * raw_multiplier)
            current_best_list[i] = current_best.detach()
            current_best_value_list[i] = -current_best_value.detach().cpu()

            if i >= iterations:
                break

            if not random_sampling:
                inner_VaR = InnerVaR(model=gp, w_samples=w_samples, alpha=alpha, dim_x=dim_x,
                                     num_lookahead_repetitions=num_lookahead_repetitions,
                                     lookahead_samples=lookahead_samples,
                                     lookahead_seed=lookahead_seed, CVaR=CVaR, expectation=expectation,
                                     beta=beta, beta_max=beta_max, cuda=cuda)

                candidate_x, candidate_x_value = optimize_acqf(acq_function=inner_VaR,
                                                               bounds=inner_bounds,
                                                               q=q,  # TODO: q>1 not implemented
                                                               num_restarts=num_restarts,
                                                               raw_samples=num_restarts * raw_multiplier)
                candidate_x_value = -candidate_x_value.detach().cpu()
                best_x_value_list[i] = candidate_x_value

            if verbose and not random_sampling:
                print('candidate_x, value: %s, %s' % (candidate_x, candidate_x_value))

            # This is the seed of fantasy model sampler. If specified the all forward passes to var_kg will share same
            # fantasy models. If None, then each forward pass will generate independent fantasies. As specified here,
            # it will be random across for loop iteration but uniform within the optimize_acqf iterations.
            # IF using SAA approach, this should be specified to a fixed number.
            fantasy_seed = int(torch.randint(100000, (1,)))

            # This has some un-explicable behavior. It keeps sampling the same point
            # w_kg = w_KG(model=gp, x_point=candidate_x, w_samples=w_samples,
            #             num_fantasies=num_fantasies,
            #             alpha=alpha, dim_x=dim_x,
            #             fantasy_seed=fantasy_seed,
            #             num_lookahead_repetitions=num_lookahead_repetitions,
            #             lookahead_samples=lookahead_samples,
            #             lookahead_seed=lookahead_seed,
            #             CVaR=CVaR, expectation=expectation)
            # if continuous:
            #     candidate_w, w_kg_value = optimize_acqf(acq_function=w_kg,
            #                                             bounds=w_bounds,
            #                                             q=q,  # TODO: q>1 not valid
            #                                             num_restarts=num_restarts,
            #                                             raw_samples=num_restarts * raw_multiplier,
            #                                             options={'maxiter': maxiter})
            # else:
            #     values = w_kg(w_samples.view((-1, 1, dim_w)))
            #     best = torch.argmax(values)
            #     candidate_w = w_samples[best].reshape(-1, dim_w)

            if not random_sampling:
                # This is the alternative based on confidence region random sampling
                candidate_w = pick_w_confidence(model=gp,
                                                beta=2,
                                                x_point=candidate_x,
                                                w_samples=w_samples,
                                                alpha=alpha,
                                                CVaR=CVaR,
                                                cuda=cuda)

                candidate = torch.cat((candidate_x, candidate_w), dim=-1).detach().cpu()
            else:
                candidate = torch.rand((q, d))
            candidate_list[i] = candidate

            if verbose:
                print("Candidate: ", candidate)
            #
            # data = {'state_dict': gp.state_dict(), 'train_Y': train_Y, 'train_X': train_X,
            #         'current_best_sol': current_best_sol, 'current_best_value': current_best_value.detach(),
            #         'candidate': candidate, 'kg_value': value.detach(),
            #         'num_samples': num_samples, 'num_fantasies': num_fantasies, 'num_restarts': num_restarts,
            #         'alpha': alpha, 'maxiter': maxiter, 'CVaR': CVaR, 'q': q,
            #         'num_lookahead_repetitions': num_lookahead_repetitions, 'lookahead_samples': lookahead_samples,
            #         'seed': seed, 'fantasy_seed': fantasy_seed, 'lookaheaad_seed': lookahead_seed,
            #         'seed_list': seed_list}
            # full_data[i] = data
            # torch.save(full_data, 'new_output/%s.pt' % filename)
            #
            iteration_end = time()
            print("Iteration %d completed in %s" % (i, iteration_end - iteration_start))

            candidate_point = candidate.reshape(q, d)
            if verbose and d == 2:
                plt.close('all')
                plotter(gp, inner_VaR, candidate_x, candidate_x_value, candidate_point,
                        w_samples, CVaR, alpha)
            observation = function(candidate_point, seed=seed_list[i])
            # update the model input data for refitting
            train_X = torch.cat((train_X, candidate_point), dim=0)
            train_Y = torch.cat((train_Y, observation), dim=0)
            passed = True

            # construct and fit the GP
            if cuda:
                gp = SingleTaskGP(train_X.cuda(), train_Y.cuda(), likelihood.cuda(),
                                  outcome_transform=Standardize(m=1)).cuda()
                mll = ExactMarginalLogLikelihood(gp.likelihood, gp).cuda()
                fit_gpytorch_model(mll).cuda()
            else:
                gp = SingleTaskGP(train_X, train_Y, likelihood, outcome_transform=Standardize(m=1))
                mll = ExactMarginalLogLikelihood(gp.likelihood, gp)
                fit_gpytorch_model(mll)

            # dummy computation to be safe with gp fit
            if cuda:
                dummy = torch.rand((1, q, d)).cuda()
            else:
                dummy = torch.rand((1, q, d))
            _ = gp.posterior(dummy).mean
        except RuntimeError as err:
            print("Runtime error %s" % err)
            print('Attempting to rerun the iteration to get around it. Seed changed for sampling.')
            handling_count += 1
            if passed:
                seed_list[i] = torch.randint(100000, (1,))
                train_X = train_X[:-q]
                train_Y = train_Y[:-q]
                if handling_count > 3:
                    try:
                        rand_X = torch.randn((q, d)) * 0.05
                        candidate_point = candidate_point + rand_X
                        observation = function(candidate_point, seed=seed_list[i])
                        train_X = torch.cat((train_X, candidate_point), dim=0)
                        train_Y = torch.cat((train_Y, observation), dim=0)
                        # construct and fit the GP
                        if cuda:
                            gp = SingleTaskGP(train_X.cuda(), train_Y.cuda(), likelihood.cuda(),
                                              outcome_transform=Standardize(m=1)).cuda()
                            mll = ExactMarginalLogLikelihood(gp.likelihood, gp).cuda()
                            fit_gpytorch_model(mll).cuda()
                        else:
                            gp = SingleTaskGP(train_X, train_Y, likelihood, outcome_transform=Standardize(m=1))
                            mll = ExactMarginalLogLikelihood(gp.likelihood, gp)
                            fit_gpytorch_model(mll)
                        # dummy computation to be safe with gp fit
                        if cuda:
                            dummy = torch.rand((1, q, d)).cuda()
                        else:
                            dummy = torch.rand((1, q, d))
                        _ = gp.posterior(dummy).mean
                    except RuntimeError:
                        print("Got another error while handling!")
                        if handling_count > 5:
                            print("Too many tries, returning None!")
                            return None
                    else:
                        i = i + 1
                        passed = False
            elif handling_count > 5:
                print("Too many tries, returning None!")
                return None
        else:
            i = i + 1
        passed = False

    print("total time: ", time() - start)
    # printing the data in case something goes wrong with file save
    # print('data: ', full_data)

    output = {'current_best': current_best_list,
              'current_best_value': current_best_value_list,
              'best_x_value': best_x_value_list,
              'candidate': candidate_list}
    return output


if __name__ == "__main__":
    # this is for momentary testing of changes to the code
    k = 100
    out = full_loop('sinequad', 0, 1, 'tester', 10,
                    num_fantasies=k, num_restarts=k, raw_multiplier=10,
                    expectation=False, verbose=False,
                    beta_c=1, beta_d=10, beta_max=0, continuous=False,
                    cuda=False,
                    random_sampling=False,
                    reporting_la_rep=0,
                    reporting_la_samples=torch.linspace(0, 1, 100).reshape(-1, 1))
    print(out)


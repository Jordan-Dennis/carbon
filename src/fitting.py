import numpy as np
import matplotlib.pyplot as plt
import jax
from jax.experimental.ode import odeint
from jax_cosmo.scipy.interpolate import InterpolatedUnivariateSpline
import celerite2.jax
from celerite2.jax import terms as jax_terms
import jax.numpy as jnp
from jax import grad, jit, random
from functools import partial
import ticktack
from astropy.table import Table
from tqdm import tqdm
import emcee
from jax.lax import cond, sub
from chainconsumer import ChainConsumer
import scipy
import seaborn as sns
from jax import jit, grad, jacrev, vmap
from jaxns.nested_sampling import NestedSampler
from jaxns.prior_transforms import PriorChain, UniformPrior
import os
from matplotlib.lines import Line2D
import matplotlib as mpl
from matplotlib.ticker import MaxNLocator

mpl.style.use('seaborn-colorblind')


class CarbonFitter:
    """
    Parent class of SingleFitter and MultiFitter. Does Monte Carlo sampling, plotting and more.
    """

    def MarkovChainSampler(self, params, likelihood, burnin=500, production=1000, k=2, args=()):
        """
        Runs an affine-invariant MCMC sampler on an array of initial parameters, subject to some likelihood function.
        Parameters
        ----------
        params : ndarray
            Initial parameters for MC sampler
        likelihood : callable
            Log-likelihood function for params
        burnin : int, optional
            Number of steps to run in burn-in period. 500 by default.
        production : int, optional
            Number of steps to run in production period. 1000 by default.
        k: int, optional
            Determines the number of walkers of the sampler via:
            nwalkers = k * dim(params)
            2 by default.
        Returns
        -------
        ndarray
            A chain of MCMC walk
        """
        initial = params
        ndim, nwalkers = len(initial), k * len(initial)
        sampler = emcee.EnsembleSampler(nwalkers, ndim, likelihood, args=args)

        print("Running burn-in...")
        p0 = initial + 1e-5 * np.random.rand(nwalkers, ndim)
        p0, lp, _ = sampler.run_mcmc(p0, burnin, progress=True);

        print("Running production...")
        sampler.reset()
        sampler.run_mcmc(p0, production, progress=True);
        return sampler.flatchain

    def NestedSampler(self, params, likelihood, low_bound=None, high_bound=None, sampler_name='multi_ellipsoid'):
        """
        Runs Nested Sampling sampler on the parameter space of some model, subject to some likelihood function.
        Parameters
        ----------
        params : ndarray
            Example parameters for NS sampler
        likelihood : callable
            Log-likelihood function for the set of parameters to be sampled
        low_bound : ndarray, optional
            Lower bound of params
        high_bound : ndarray, optional
            Upper bound of params
        sampler_name : str, optional
            Name of sampling method. Take value in ['multi_ellipsoid', 'slice']. 'multi_ellipsoid' by default.
        Returns
        -------
        ndarray
            A chain of MCMC walk
        """

        @jit
        def likelihood_function(params, **kwargs):
            return likelihood(params)

        ndim = params.size
        if low_bound is not None and high_bound is not None:
            low_bound = jnp.array(low_bound)
            high_bound = jnp.array(high_bound)
        else:
            low_bound = jnp.array(ndim * None)
            high_bound = jnp.array(ndim * None)
        prior_chain = PriorChain().push(UniformPrior('params', low=low_bound, high=high_bound))
        ns = NestedSampler(likelihood_function, prior_chain, num_live_points=100 * prior_chain.U_ndims,
                           sampler_name=sampler_name)
        results = jit(ns)(key=random.PRNGKey(0))
        # summary(results)
        return results

    def chain_summary(self, chain, walkers, figsize=(10, 10), labels=None, plot_dist=False, test_convergence=False,
                      label_font_size=8, tick_font_size=8, mle=False):
        """
        From a chain of MCMC walks apply convergence test and plot posterior surfaces of parameters
        Parameters
        ----------
        chain : ndarray
            A chain of MCMC walks
        walkers : int
            The number of walkers for the chain
        figsize : tuple, optional
            Output figure size
        labels : list[str], optional
            A list of parameter names
        plot_dist : bool, optional
            If True, only plot the marginal distributions of parameters
        Returns
        -------
        """
        if labels:
            c = ChainConsumer().add_chain(chain, walkers=walkers, parameters=labels)
        else:
            c = ChainConsumer().add_chain(chain, walkers=walkers)

        if test_convergence:
            gelman_rubin_converged = c.diagnostic.gelman_rubin()
            geweke_converged = c.diagnostic.geweke()
            if gelman_rubin_converged and geweke_converged:
                self.convergence = True
            else:
                self.convergence = False
            print("Convergence: %s" % self.convergence)

        if plot_dist:
            fig = c.plotter.plot_distributions(figsize=figsize)
        else:
            c.configure(spacing=0.0, usetex=False, label_font_size=label_font_size, tick_font_size=tick_font_size,
                        diagonal_tick_labels=False, )
            fig = c.plotter.plot(figsize=figsize)
        if mle:
            MLE = []
            for lst in c.analysis.get_summary().values():
                MLE.append(lst[1])
            return MLE

    def correlation_plot(self, array, figsize=10, square_size=100):
        """
        Makes an accessible heatmap for visualizing correlation/covariance matrix.
        Parameters
        ----------
        array : ndarray
            n x n matrix for the heatmap
        figsize : int, optional
            Controls the size of the output figure. Should increase with the size of 'array'. 10 by default
        square_size: int, optional
            Controls the size of squares in the heatmap. Should decrease with the size of 'array'. 100 by default
        Returns
        -------
        figure
            heatmap
        """
        assert array.shape[0] == array.shape[1], "array must be a square (n x n) matrix"
        n = array.shape[0]
        arr = array.reshape(-1)
        size = np.abs(arr)
        x = np.repeat(np.arange(n), n)
        y = (n - 1) - np.tile(np.arange(n), n)

        plot_grid = plt.GridSpec(1, 10, hspace=0.2, wspace=0.1)
        ax = plt.subplots(figsize=(figsize, figsize))
        ax = plt.subplot(plot_grid[:, :-1])

        n_colors = 256
        palette = sns.diverging_palette(10, 220, n=n_colors)

        if np.min(array) < 0:
            bounds = np.abs(np.max((np.min(array), np.max(array))))
            color_min, color_max = (-bounds, bounds)
        else:
            color_min, color_max = (np.min(array), np.max(array))

        def value_to_color(val):
            val_position = (val - color_min) / (color_max - color_min)
            ind = int(val_position * (n_colors - 1))
            return palette[ind]

        ax.scatter(
            x=x, y=y, s=size * square_size, c=[value_to_color(i) for i in arr], marker='s')
        ax.set_xticks(np.unique(x));
        ax.set_yticks(np.unique(x));

        ax.set_xticklabels(np.unique(x));
        ax.set_yticklabels(reversed(np.unique(x)));

        ax.grid(False, 'major')
        ax.grid(True, 'minor')
        ax.set_xticks([t + 0.5 for t in ax.get_xticks()], minor=True)
        ax.set_yticks([t + 0.5 for t in ax.get_yticks()], minor=True)
        ax.set_xlim([-0.5, (n - 1) + 0.5]);
        ax.set_ylim([-0.5, (n - 1) + 0.5]);

        ax = plt.subplot(plot_grid[:, -1])
        col_x = [0] * len(palette)
        bar_y = np.linspace(color_min, color_max, n_colors)
        bar_height = bar_y[1] - bar_y[0]
        ax.barh(
            y=bar_y,
            width=[5] * len(palette),
            left=col_x,
            height=bar_height,
            color=palette,
            linewidth=0
        )
        ax.set_xlim(1, 2)
        ax.set_ylim(color_min - 0.001, color_max + 0.001)
        ax.grid(False)
        ax.set_facecolor('white')
        ax.set_xticks([])
        ax.set_yticks([color_min, (color_min + color_max) / 2, color_max])
        ax.yaxis.tick_right()

    def plot_multiple_chains(self, chains, walker, figsize=(10, 10), title=None, params_labels=None, labels=None,
                             colors=None,
                             alpha=0.5, linewidths=None, plot_dists=False, label_font_size=12, tick_font_size=8,
                             max_ticks=10, legend=True):
        """
       Overplots posterior surfaces of parameters from multiple chains.
        Parameters
        ----------
        chains : list
            List of chains of MCMC walks
        walker : int
            The number of walkers for each chain in 'chains'
        figsize : tuple, optional
            Output figure size
        params_labels : list[str], optional
            A list of parameter names
        labels : list[str], optional
            Labels that distinguish different chains
        colors : list[str], optional
            A list of color names, used to distinguish different chains
        alpha : float, optional
            Parameter for blending, between 0-1.
        linewidths : float, optional
            Line width, in points
        plot_dists : bool, optional
            If True, only plot the marginal distributions of parameters
        Returns
        -------
        figure
            plot of posterior surfaces or marginal distributions
        """
        c = ChainConsumer()
        if labels:
            assert len(labels) == len(chains), "labels must have the same length as chains"
            for i in range(len(chains)):
                c.add_chain(chains[i], walkers=walker, parameters=params_labels, name=labels[i])
        else:
            for i in range(len(chains)):
                c.add_chain(chains[i], walkers=walker, parameters=params_labels)
        c.configure(colors=colors, shade_alpha=alpha, linewidths=linewidths, usetex=False,
                    label_font_size=label_font_size, tick_font_size=tick_font_size, diagonal_tick_labels=False,
                    max_ticks=max_ticks)
        # legend_kwargs={"fontsize":14}

        if plot_dists:
            fig = c.plotter.plot_distributions(figsize=figsize, legend=legend)
        else:
            fig = c.plotter.plot(figsize=figsize, legend=legend)
        plt.suptitle(title)
        plt.tight_layout()
        return fig

    def plot_samples(self, average_path=None, chains_path=None, cbm_models=None, hemisphere="north",
                     production_model=None,
                     directory_path=None, size=100, size2=30, alpha=0.05, alpha2=0.2, savefig_path=None):
        colors = mpl.rcParams['axes.prop_cycle'].by_key()['color']
        for i, model in enumerate(cbm_models):
            cbm = ticktack.load_presaved_model(model, production_rate_units='atoms/cm^2/s')
            sf = fitting.SingleFitter(cbm, cbm_model=model, hemisphere=hemisphere)
            sf.load_data(average_path)
            chain = np.load(chains_path[i])
            sf.compile_production_model(model=production_model)

            idx = np.random.randint(len(chain), size=size)
            for param in chain[idx]:
                ax1.plot(sf.time_data_fine, sf.dc14_fine(params=param), alpha=alpha, color=colors[i])

            ax1.set_ylabel("$\Delta^{14}$C (‰)")
            fig.subplots_adjust(hspace=0.05)

            for param in chain[idx][:size2]:
                ax2.plot(sf.time_data_fine, sf.production(sf.time_data_fine, *param), alpha=alpha2, color=colors[i])

        ax1.errorbar(sf.time_data, sf.d14c_data, yerr=sf.d14c_data_error, fmt="ok", capsize=3,
                     markersize=6.5, elinewidth=3, label="average $\Delta^{14}$C")

        if directory_path:
            file_names = [f for f in os.listdir(directory_path) if os.path.isfile(os.path.join(directory_path, f))]
            for file in file_names:
                sf.load_data(directory_path + '/' + file)
                ax1.errorbar(sf.time_data, sf.d14c_data, fmt="o", color="gray", yerr=sf.d14c_data_error, capsize=3,
                             alpha=0.2)
                ax1.plot(sf.time_data, sf.d14c_data, "o", color="gray", alpha=0.2)

        custom_lines = [Line2D([0], [0], color=colors[i], lw=1.5, label=models[i]) for i in range(len(cbm_models))]
        custom_lines.append(Line2D([0], [0], color="k", marker="o", lw=1.5, label="average $\Delta^{14}$C"))
        ax1.legend(handles=custom_lines)
        ax2.set_ylim(1, 10);
        ax2.set_xlabel("Calendar Year (CE)");
        ax2.set_xlim(sf.start - 0.2, sf.end + 0.2);
        ax2.set_ylabel("Production rate (atoms/cm$^2$/s)");
        ax2.legend(loc="upper left")
        plt.suptitle(event);
        plt.tight_layout();
        if savefig_path:
            plt.savefig(savefig_path)


class SingleFitter(CarbonFitter):
    """
    A class for parametric and non-parametric inference of d14c data using a Carbon Box Model (cbm).
    Does parameter fitting, Monte Carlo sampling, plotting and more.
    """

    def __init__(self, cbm, cbm_model, production_rate_units='atoms/cm^2/s', target_C_14=707., box='Troposphere',
                 hemisphere='north'):
        """
        Initializes a SingleFitter Object
        Parameters
        ----------
        cbm : CarbonBoxModel Object
            A Carbon Box Model
        production_rate_units : str, optional
            The production rate units of the cbm. 'atoms/cm^2/s' by default
        target_C_14 : float, optional
            target 14C content for equilibration, 707 by default
        box : str, optional
            The specific box at which to calculate the d14c. 'Troposphere' by default
        hemisphere : str, optional
            The hemisphere which the SingleFitter object will model, can take values in
            ['north', 'south']. 'north' by default
        Returns
        -------
        """
        if isinstance(cbm, str):
            try:
                if cbm in ['Guttler15', 'Brehm21', 'Miyake17', 'Buntgen18']:
                    cbm = ticktack.load_presaved_model(cbm, production_rate_units=production_rate_units)
                else:
                    cbm = ticktack.load_model(cbm, production_rate_units=production_rate_units)
            except:
                raise ValueError('Must be a valid CBM model')
        self.cbm = cbm
        self.cbm.compile()
        self.box = box
        if hemisphere in ['south', 'north']:
            self.hemisphere = hemisphere
        else:
            raise ValueError("'hemisphere' be one of the following: south, north")
        self.cbm_model = cbm_model

        if cbm_model in ['Brehm21', 'Buntgen18']:
            self.steady_state_production = 1.76
            self.steady_state_y0 = self.cbm.equilibrate(production_rate=self.steady_state_production)
            for i, node in enumerate(cbm.get_nodes_objects()):
                if (node.get_hemisphere() == self.hemisphere) & (node.get_name() == self.box):
                    self.box_idx = i
        elif cbm_model == "Miyake17":
            self.steady_state_production = 1.8
            self.steady_state_y0 = self.cbm.equilibrate(production_rate=self.steady_state_production)
            self.box_idx = 1
        elif cbm_model == "Guttler15":
            self.steady_state_production = self.cbm.equilibrate(target_C_14=target_C_14)
            self.steady_state_y0 = self.cbm.equilibrate(production_rate=self.steady_state_production)
            self.box_idx = 1

    def load_data(self, file_name, oversample=1008, burnin_oversample=1, burnin_time=2000, num_offset=4):
        """
        Loads d14c data from specified file
        Parameters
        ----------
        file_name : str
            Path to the file
        resolution : int, optional
            1000 by default
        fine_grid : float, optional
            0.05 by default
        oversample : int, optional
            1000 by default
        num_offset : int, optional
            When set to x the first x data points are averaged to compute an offset, which will be subtracted from
            all data points
        Returns
        -------
        """
        data = Table.read(file_name, format="ascii")
        self.time_data = jnp.array(data["year"])
        self.d14c_data = jnp.array(data["d14c"])
        self.d14c_data_error = jnp.array(data["sig_d14c"])
        self.start = np.nanmin(self.time_data)
        self.end = np.nanmax(self.time_data)
        self.burn_in_time = jnp.arange(self.start - burnin_time, self.start + 1, 1.)
        self.oversample = oversample
        self.burnin_oversample = burnin_oversample
        self.offset = jnp.mean(self.d14c_data[:num_offset])
        self.annual = jnp.arange(self.start, self.end + 1)
        self.mask = jnp.in1d(self.annual, self.time_data)
        self.time_data_fine = jnp.linspace(jnp.min(self.annual), jnp.max(self.annual) + 2,
                                           (self.annual.size + 1) * self.oversample)
        if self.hemisphere == 'north':
            self.growth = self.get_growth_vector("april-september")
        else:
            self.growth = self.get_growth_vector("october-march")
        try:
            self.growth = self.get_growth_vector(data["growth_season"][0])
        except:
            pass

    def get_growth_vector(self, growth_season):
        """
        Converts the growing season of a tree from string to 12-digit binary vector
        Parameters
        ----------
        growth_season : str
            Growing season of a tree. Must have the following format: "StartMonth-EndMonth"
        Returns
        -------
        ndarray
            12-digit binary vector of growing season
        """
        growth_dict = {"january": 0, "february": 1, "march": 2, "april": 3, "may": 4,
                       "june": 5, "july": 6, "august": 7, "september": 8, "october": 9,
                       "november": 10, "december": 11}
        start = growth_dict[growth_season.split('-')[0].lower()]
        end = growth_dict[growth_season.split('-')[1].lower()]
        growth = np.zeros((12,))
        if end < start:
            growth[start:] = 1
            growth[:end + 1] = 1
            self.time_offset = (start / 2 + end / 2 + 6) / 12
        else:
            growth[start:end + 1] = 1
            self.time_offset = (start / 2 + end / 2) / 12
        return jnp.array(growth)

    def compile_production_model(self, model=None):
        """
        Specifies the production rate model
        Parameters
        ----------
        model : str | callable, optional
            Specifies a built-in model or a custom model. Currently supported built-in models include ["simple_sinusoid",
            "flexible_sinusoid", "flexible_sinusoid_affine_variant", "control_points"]
        Returns
        -------
        """
        self.production = None
        if callable(model):
            self.production = model
            self.production_model = 'custom'
        elif model == "simple_sinusoid":
            self.production = self.simple_sinusoid
            self.production_model = 'simple sinusoid'
        elif model == "simple_sinusoid_sharp":
            self.production = self.simple_sinusoid_sharp
        elif model == "simple_sinusoid_prolonged":
            self.production = self.simple_sinusoid_prolonged
        elif model == "flexible_sinusoid":
            self.production = self.flexible_sinusoid
            self.production_model = 'flexible sinusoid'
        elif model == "flexible_sinusoid_affine_variant":
            self.production = self.flexible_sinusoid_affine_variant
            self.production_model = 'flexible sinusoid affine variant'
        elif model == "affine":
            self.production = self.affine
            self.production_model = 'affine'
        elif model == "control_points":
            self.control_points_time = jnp.arange(self.start, self.end)
            self.control_points_time_fine = jnp.linspace(self.start, self.end, int((self.end - self.start) * self.oversample))
            self.production = self.interp_gp
            self.production_model = 'control points'
        else:
            raise ValueError(
                "model is not a callable, or does not take value from: simple_sinusoid, flexible_sinusoid, flexible_sinusoid_affine_variant, control_points")

    @partial(jit, static_argnums=(0,))
    def interp_gp(self, tval, *args):
        """
        A Gaussian Process regression interpolator
        Parameters
        ----------
        tval : ndarray
            Time sampling of the output interpolation
        args : ndarray | float
            Set of control-points. Can be passed in as ndarray or individual floats. Must have the same size as
            self.control_points_time.
        Returns
        -------
        ndarray
            Interpolation on tval
        """
        tval = tval.reshape(-1)
        params = jnp.array(list(args)).reshape(-1)
        mean = params[0]
        kernel = jax_terms.Matern32Term(sigma=2., rho=2.)
        gp = celerite2.jax.GaussianProcess(kernel, self.control_points_time, mean=mean)
        alpha = gp.apply_inverse(params)
        Ks = kernel.get_value(tval[:, None] - self.control_points_time[None, :])
        mu = jnp.dot(Ks, alpha)
        mu = (tval > self.start) * mu + (tval <= self.start) * mean
        return mu

    @partial(jit, static_argnums=(0,))
    def super_gaussian(self, t, start_time, duration, area):
        """
        Computes the density of a super gaussian function with an exponent of 16. Emulates the
        spike in d14c data following the occurrence of a Miyake event
        Parameters
        ----------
        t : ndarray
            Scalar or vector input
        start_time : float
            Start time of a Miyake event
        duration : float
            Duration of a Miyake event
        area : float
            Total radiocarbon delivered by a Miyake event
        Returns
        -------
        ndarray
            Super gaussian density
        """
        middle = start_time + duration / 2.
        height = area / duration
        return height * jnp.exp(- ((t - middle) / (1. / 1.93516 * duration)) ** 16.)

    @partial(jit, static_argnums=(0,))
    def simple_sinusoid(self, t, *args):
        """
        A simple sinusoid model for production rates over a period where a Miyake event is observed. Tunable parameters
        include:
        Start time: start time of the Miyake event
        Duration: duration of the Miyake event
        Phase: phase of the solar cycle during this period
        Area: total radiocarbon delivered by this Miyake event (in production rate times years)
        Parameters
        ----------
        t : ndarray
            Time values. Scalar or vector input
        args : ndarray | float
            Can be passed in as a ndarray or as individual floats. Must include start time, duration, phase and area
        Returns
        -------
        ndarray
            Production rate on t
        """
        start_time, duration, phase, area = jnp.array(list(args)).reshape(-1)
        height = self.super_gaussian(t, start_time, duration, area)
        production = self.steady_state_production + 0.18 * self.steady_state_production * jnp.sin(
            2 * np.pi / 11 * t + phase * 2 * np.pi / 11) + height
        return production

    @partial(jit, static_argnums=(0,))
    def simple_sinusoid_sharp(self, t, *args):
        """
        A simple sinusoid model for production rates over a period where a Miyake event is observed. Tunable parameters
        include:
        Start time: start time of the Miyake event
        Duration: duration of the Miyake event
        Phase: phase of the solar cycle during this period
        Area: total radiocarbon delivered by this Miyake event (in production rate times years)
        Parameters
        ----------
        t : ndarray
            Time values. Scalar or vector input
        args : ndarray | float
            Can be passed in as a ndarray or as individual floats. Must include start time, duration, phase and area
        Returns
        -------
        ndarray
            Production rate on t
        """
        start_time, duration, area = jnp.array(list(args)).reshape(-1)
        height = self.super_gaussian(t, start_time, duration, area)
        production = self.steady_state_production + 1.580932349731588, * self.steady_state_production * jnp.sin(
            2 * np.pi / 11 * t + 0.1463655781098268 * 2 * np.pi / 11) + height
        return production

    @partial(jit, static_argnums=(0,))
    def simple_sinusoid_prolonged(self, t, *args):
        """
        A simple sinusoid model for production rates over a period where a Miyake event is observed. Tunable parameters
        include:
        Start time: start time of the Miyake event
        Duration: duration of the Miyake event
        Phase: phase of the solar cycle during this period
        Area: total radiocarbon delivered by this Miyake event (in production rate times years)
        Parameters
        ----------
        t : ndarray
            Time values. Scalar or vector input
        args : ndarray | float
            Can be passed in as a ndarray or as individual floats. Must include start time, duration, phase and area
        Returns
        -------
        ndarray
            Production rate on t
        """
        start_time, duration, area = jnp.array(list(args)).reshape(-1)
        height = self.super_gaussian(t, start_time, duration, area)
        production = self.steady_state_production + 0.10734924539181684 * self.steady_state_production * jnp.sin(
            2 * np.pi / 11 * t + 0.06395878249485243 * 2 * np.pi / 11) + height
        return production

    @partial(jit, static_argnums=(0,))
    def flexible_sinusoid(self, t, *args):
        """
        A flexible sinusoid model for production rates in a period where a Miyake event is observed. Tunable parameters
        include:
        Start time: start time of the Miyake event
        Duration: duration of the Miyake event
        Phase: phase of the solar cycle during this period
        Area: total radiocarbon delivered by this Miyake event (in production rate times years)
        Amplitude: Amplitude of the solar cycle during this period
        Parameters
        ----------
        t : ndarray
            Time values. Scalar or vector input
        args : ndarray | float
            Can be passed in as a ndarray or as individual floats. Must include start time, duration, phase, area and
            amplitude
        Returns
        -------
        ndarray
            Production rate on t
        """
        start_time, duration, phase, area, amplitude = jnp.array(list(args)).reshape(-1)
        height = self.super_gaussian(t, start_time, duration, area)
        production = self.steady_state_production + amplitude * self.steady_state_production * jnp.sin(
            2 * np.pi / 11 * t + phase * 2 * np.pi / 11) + height
        return production

    @partial(jit, static_argnums=(0,))
    def flexible_sinusoid_affine_variant(self, t, *args):
        gradient, start_time, duration, phase, area, amplitude = jnp.array(list(args)).reshape(-1)
        height = self.super_gaussian(t, start_time, duration, area)
        production = self.steady_state_production + gradient * (
                t - self.start) * (t >= self.start) + amplitude * self.steady_state_production * jnp.sin(
            2 * np.pi / 11 * t
            + phase * 2 * np.pi / 11) + height
        return production

    @partial(jit, static_argnums=(0,))
    def affine(self, t, *args):
        gradient, start_time, duration, area = jnp.array(list(args)).reshape(-1)
        height = self.super_gaussian(t, start_time, duration, area)
        production = self.steady_state_production + gradient * (t - self.start) * (t >= self.start) + height
        return production

    @partial(jit, static_argnums=(0))
    def run_burnin(self, y0=None, params=()):
        """
        Calculates the C14 content of all the boxes within a carbon box model for the burn-in period.
        Parameters
        ----------
        time_values : ndarray
            Time values
        y0 : ndarray, optional
            The initial contents of all boxes
        params : ndarray, optional
            Parameters for self.production
        Returns
        -------
        ndarray
            The value of each box in the carbon box at the specified time_values along with the steady state solution
            for the system
        """
        box_values, _ = self.cbm.run(self.burn_in_time, self.production, y0=y0, args=params,
                                     steady_state_production=self.steady_state_production)
        return box_values

    @partial(jit, static_argnums=(0))
    def run_event(self, y0=None, params=()):
        """
        Calculates the C14 content of all the boxes within a carbon box model for the event duration.
        Parameters
        ----------
        time_values : ndarray
            Time values
        y0 : ndarray, optional
            The initial contents of all boxes
        params : ndarray, optional
            Parameters for self.production
        Returns
        -------
        ndarray
            The value of each box in the carbon box at the specified time_values along with the steady state solution
            for the system
        """
        time_values = jnp.linspace(jnp.min(self.annual), jnp.max(self.annual) + 2,
                                   (self.annual.size + 1) * self.oversample)
        box_values, _ = self.cbm.run(time_values, self.production, y0=y0, args=params,
                                     steady_state_production=self.steady_state_production)
        return box_values

    @partial(jit, static_argnums=(0,))
    def dc14(self, params=()):
        """
        Predict d14c on the same time sampling as self.time_data
        Parameters
        ----------
        params : ndarray, optional
            Parameters for self.production
        Returns
        -------
        ndarray
            Predicted d14c value
        """
        burnin = self.run_burnin(y0=self.steady_state_y0, params=params)
        event = self.run_event(y0=burnin[-1, :], params=params)
        binned_data = self.cbm.bin_data(event[:, self.box_idx], self.oversample, self.annual, growth=self.growth)
        d14c = (binned_data - self.steady_state_y0[self.box_idx]) / self.steady_state_y0[self.box_idx] * 1000
        return d14c[self.mask] + self.offset

    @partial(jit, static_argnums=(0,))
    def dc14_fine(self, params=()):
        """
        Predict d14c on the same time sampling as self.time_data_fine.
        Parameters
        ----------
        params : ndarray, optional
            Parameters for self.production
        Returns
        -------
        ndarray
            Predicted d14c value
        """
        burnin = self.run_burnin(y0=self.steady_state_y0, params=params)
        event = self.run_event(y0=burnin[-1, :], params=params)
        d14c = (event[:, self.box_idx] - self.steady_state_y0[self.box_idx]) / self.steady_state_y0[self.box_idx] * 1000
        return d14c + self.offset

    # @partial(jit, static_argnums=(0,))
    def log_likelihood(self, params=()):
        """
        Computes the gaussian log-likelihood of parameters of self.production
        Parameters
        ----------
        params : ndarray, optional
            Parameters of self.production
        Returns
        -------
        float
            Gaussian log-likelihood
        """
        d14c = self.dc14(params)
        return -0.5 * jnp.sum(((self.d14c_data - d14c) / self.d14c_data_error) ** 2)

    @partial(jit, static_argnums=(0,))
    def log_joint_likelihood(self, params, low_bounds, up_bounds):
        """
        Computes the log joint likelihood of parameters of self.production
        Parameters
        ----------
        params : ndarray
            Parameters of self.production
        low_bounds : ndarray
            Lower bound for the parameters of self.production
        up_bounds : ndarray
            Upper bound for the parameters of self.production
        Returns
        -------
        float
            Log joint likelihood
        """
        lp = 0
        lp += jnp.any((params < low_bounds) | (params > up_bounds)) * -jnp.inf
        pos = self.log_likelihood(params)
        return lp + pos

    @partial(jit, static_argnums=(0,))
    def log_likelihood_gp(self, params):
        """
        Computes the Gaussian Process log-likelihood of a set of control-points. The Gaussian Process
        has a constant mean and a Matern-3/2 kernel with fixed parameters.
        Parameters
        ----------
        params : ndarray
            An array of control-points. First control point is also the mean of the Gaussian Process
        Returns
        -------
        float
            Gaussian Process log-likelihood
        """
        kernel = jax_terms.Matern32Term(sigma=2., rho=2.)
        gp = celerite2.jax.GaussianProcess(kernel, mean=params[0])
        gp.compute(self.control_points_time)
        return gp.log_likelihood(params)

    @partial(jit, static_argnums=(0,))
    def log_joint_likelihood_gp(self, params, low_bounds, up_bounds):
        """
        Computes the negative log joint likelihood of a set of control-points. Used as the objective function for
        training the set of control-points via numerical optimization.
        Parameters
        ----------
        params : ndarray
            An array of control-points. First control point is also the mean of the Gaussian Process
        Returns
        -------
        float
            Negative log joint likelihood
        """
        lp = jnp.any((params < low_bounds) | (params > up_bounds)) * -jnp.inf
        return self.log_likelihood(params=params) + self.log_likelihood_gp(params) + lp

    @partial(jit, static_argnums=(0,))
    def neg_log_joint_likelihood_gp(self, params):
        """
        Computes the negative log joint likelihood of a set of control-points. Used as the objective function for
        training the set of control-points via numerical optimization.
        Parameters
        ----------
        params : ndarray
            An array of control-points. First control point is also the mean of the Gaussian Process
        Returns
        -------
        float
            Negative log joint likelihood
        """
        return -1 * self.log_likelihood(params=params) + -1 * self.log_likelihood_gp(params)

    @partial(jit, static_argnums=(0,))
    def grad_neg_log_joint_likelihood_gp(self, params=()):
        """
        Computes the negative gradient of the log joint likelihood of control-points.
        Parameters
        ----------
        params : ndarray
            An array of control-points. First control point is also the mean of the Gaussian Process
        Returns
        -------
        float
            Negative gradient of log joint likelihood
        """
        return grad(self.neg_log_joint_likelihood_gp)(params)

    def fit_ControlPoints(self, low_bound=0):
        """
        Fits the control-points by minimizing the negative log joint likelihood.
        Parameters
        ----------
        low_bound : int, optional
            The minimum value each control-point can take. 0 by default.
        Returns
        -------
        OptimizeResult
            Scipy OptimizeResult object
        """
        initial = self.steady_state_production * jnp.ones((len(self.control_points_time),))
        bounds = tuple([(low_bound, None)] * len(initial))
        soln = scipy.optimize.minimize(self.neg_log_joint_likelihood_gp, initial, bounds=bounds,
                                       options={'maxiter': 100000, 'maxfun': 100000, })
        return soln

    # @partial(jit, static_argnums=(0,))
    # def sum_interp_gp(self, *args):
    #     mu = self.interp_gp(self.annual, *args)
    #     return jnp.sum(mu)
    #
    # @partial(jit, static_argnums=(0,))
    # def grad_sum_interp_gp(self, *args):
    #     return grad(self.sum_interp_gp)(*args)

    @partial(jit, static_argnums=(0, 5, 6))
    def reconstruct_production_rate(self, d14c, t_in, t_out, steady_state_solution, steady_state_production=None,
                                    target_C_14=None):

        data = d14c / 1000 * steady_state_solution[self.box_idx] + steady_state_solution[self.box_idx]
        first1 = jnp.where(self.growth == 1, size=1)[0][0]
        first0 = jnp.where(self.growth == 0, size=1)[0][0]
        all1s = jnp.where(self.growth == 1, size=12)[0]
        after1 = jnp.where(all1s > first0, all1s, 0)
        after1 = after1.at[jnp.nonzero(after1, size=1)].get()[0]
        num = sub(first1, after1)
        val = cond(num == 0, lambda x: first1, lambda x: after1, num)
        act = cond(jnp.all(self.growth == 1), lambda x: 0, lambda x: val, self.growth)
        act = act + jnp.count_nonzero(self.growth)/2
        t_in = t_in + act / 12

        @jit
        def interp(time):
            fn = cond(jnp.count_nonzero(self.growth) == 12, lambda x: jnp.interp(time, t_in, data),
                      lambda x: InterpolatedUnivariateSpline(t_in, data)(time), self.growth)
            return fn

        dash = jit(grad(interp))

        @partial(jit, static_argnums=(0))
        def _reverse_convert_production_rate(cbm, production_rate):
            new_rate = None
            if cbm._production_rate_units == 'atoms/cm^2/s':
                new_rate = production_rate / (14.003242 / 6.022 * 5.11 * 31536. / 1.e5)
            elif cbm._production_rate_units == 'kg/yr':
                new_rate = production_rate
            return new_rate

        @jit
        def derivative(y, time):
            ans = jnp.matmul(self.cbm.get_matrix(), y)
            prod_coeff = self.cbm.get_production_coefficients()
            production_rate = (dash(time) - ans[self.box_idx]) / prod_coeff[self.box_idx]
            production_term = prod_coeff * production_rate
            return ans + production_term

        if target_C_14 is not None:
            steady_state = self.cbm.equilibrate(production_rate=self.cbm.equilibrate(target_C_14=target_C_14))
        elif steady_state_production is not None:
            steady_state = self.cbm.equilibrate(production_rate=steady_state_production)
        else:
            raise ValueError("Must give either target C-14 or production rate.")

        states = odeint(derivative, steady_state, t_out, atol=1e-15, rtol=1e-15)

        flows = jnp.matmul(self.cbm.get_matrix(), states.T)
        return _reverse_convert_production_rate(self.cbm, (vmap(dash)(t_out) - flows[self.box_idx, :]) /
                                                self.cbm.get_production_coefficients()[self.box_idx])

    def MC_mean_std(self, iters=1000, t_in=None, t_out=None):
        if t_in is None:
            t_in = jnp.zeros((self.time_data.size + 1))
            t_in = jax.ops.index_update(t_in, jnp.arange(self.time_data.size + 1)[1:], self.time_data)
            t_in = jax.ops.index_update(t_in, 0, self.start - 1)

        if t_out is None:
            t_out = self.time_data.astype('float64')
            # t_out = jnp.arange(jnp.min(t_out) - 1, jnp.max(t_out) + 1)

        production_rates = []

        for _ in tqdm(range(iters)):
            new_data = self.d14c_data - self.offset + np.random.randn(len(self.d14c_data)) * self.d14c_data_error
            new_data = np.concatenate((jnp.expand_dims(jnp.array(0), axis=0), new_data))
            prod_recon = self.reconstruct_production_rate(new_data, t_in, t_out, self.steady_state_y0,
                                                          steady_state_production=self.steady_state_production)
            production_rates.append(prod_recon)

        chain = np.array(production_rates)
        return chain


class MultiFitter(CarbonFitter):
    """
    A class for parametric inference of d14c data from a common time period using an ensemble of SingleFitter.
    Does parameter fitting, likelihood evaluations, Monte Carlo sampling, plotting and more.
    """

    def __init__(self):
        """
        Initializes a MultiFitter object. If sf is not None it should be a list of SingleFitter objects.
        Parameters
        ----------
        params : ndarray
            Parameters of a parametric model
        Returns
        -------
        float
            Log-likelihood
        """
        self.MultiFitter = []
        self.burnin_oversample = 1
        self.start = None
        self.end = None
        self.oversample = None
        self.production = None
        self.production_model = None
        self.burn_in_time = None
        self.steady_state_y0 = None
        self.steady_state_production = None
        self.growth = None
        self.cbm = None
        self.cbm_model = None
        self.box_idx = None

    def add_SingleFitter(self, sf):
        """
        Adds a SingleFitter Object to a Multifitter Object
        Parameters
        ----------
        sf : SingleFitter
            SingleFitter Object
        Returns
        -------
        """
        if not self.start:
            self.start = sf.start
        elif self.start > sf.start:
            self.start = sf.start

        if not self.end:
            self.end = sf.end
        elif self.end < sf.end:
            self.end = sf.end

        if self.production is None:
            self.production = sf.production
            self.production_model = sf.production_model
        elif self.production_model is not sf.production_model:
            raise ValueError(
                "production for SingleFitters must be consistent. Got {}, expected {}".format(sf.production_model,
                                                                                              self.production_model))

        if self.oversample is None:
            self.oversample = sf.oversample
        elif self.oversample < sf.oversample:
            self.oversample = sf.oversample

        if not self.box_idx:
            self.box_idx = sf.box_idx
        elif not self.box_idx == sf.box_idx:
            raise ValueError(
                "'box' parameter and 'hemisphere' parameter for SingleFitters must be consistent")

        if self.steady_state_y0 is None:
            self.steady_state_y0 = sf.steady_state_y0
            self.steady_state_production = sf.steady_state_production
        elif not jnp.allclose(self.steady_state_y0, sf.steady_state_y0):
            raise ValueError(
                "steady state burn-in solution for SingleFitters must be consistent. Got {}, expected {}".format(
                    sf.steady_state_y0,
                    self.steady_state_y0))

        if self.cbm is None:
            self.cbm_model = sf.cbm_model
            self.cbm = ticktack.load_presaved_model(self.cbm_model, production_rate_units='atoms/cm^2/s')
            self.cbm.compile()
        elif not self.cbm_model is sf.cbm_model:
            raise ValueError("cbm model for SingleFitters must be consistent. Got {}, expected {}".format(sf.cbm_model,
                                                                                                          self.cbm_model))
        self.MultiFitter.append(sf)

    def compile(self):
        """
        Prepares a Multifitter Object for d14c computation and likelihood evaluation
        Parameters
        ----------
        sf : SingleFitter
            SingleFitter Object
        Returns
        -------
        """
        self.burn_in_time = jnp.arange(self.start - 2000, self.start + 1, 1.)
        self.annual = jnp.arange(self.start, self.end + 1)
        self.time_data_fine = jnp.linspace(jnp.min(self.annual), jnp.max(self.annual) + 2,
                                           (self.annual.size + 1) * self.oversample)
        for sf in self.MultiFitter:
            sf.multi_mask = jnp.in1d(self.annual, sf.time_data)
        if self.production_model == 'control points':
            self.control_points_time = jnp.arange(self.start, self.end)
            self.production = self.multi_interp_gp
        self.steady_state_box = self.steady_state_y0[self.box_idx]

    @partial(jit, static_argnums=(0,))
    def multi_interp_gp(self, tval, *args):
        """
        A Gaussian Process regression interpolator for MultiFitter
        Parameters
        ----------
        tval : ndarray
            Time sampling of the output interpolation
        args : ndarray | float
            Set of control-points. Can be passed in as ndarray or individual floats. Must have the same size as
            self.control_points_time.
        Returns
        -------
        ndarray
            Interpolated values on tval
        """
        tval = tval.reshape(-1)
        params = jnp.array(list(args)).reshape(-1)
        mean = params[0]
        kernel = jax_terms.Matern32Term(sigma=2., rho=2.)
        gp = celerite2.jax.GaussianProcess(kernel, self.control_points_time, mean=mean)
        alpha = gp.apply_inverse(params)
        Ks = kernel.get_value(tval[:, None] - self.control_points_time[None, :])
        mu = jnp.dot(Ks, alpha)
        mu = (tval > self.start) * mu + (tval <= self.start) * mean
        return mu

    @partial(jit, static_argnums=(0))
    def run_burnin(self, y0=None, params=()):
        """
        Calculates the C14 content of all the boxes within a carbon box model for the burn-in period.
        Parameters
        ----------
        time_values : ndarray
            Time values
        y0 : ndarray, optional
            The initial contents of all boxes
        params : ndarray, optional
            Parameters for self.production
        Returns
        -------
        ndarray
            The value of each box in the carbon box at the specified time_values along with the steady state solution
            for the system
        """
        box_values, _ = self.cbm.run(self.burn_in_time, self.production, y0=y0, args=params,
                                     steady_state_production=self.steady_state_production)
        return box_values

    @partial(jit, static_argnums=(0))
    def run_event(self, y0=None, params=()):
        """
        Calculates the C14 content of all the boxes within a carbon box model for the event duration.
        Parameters
        ----------
        time_values : ndarray
            Time values
        y0 : ndarray, optional
            The initial contents of all boxes
        params : ndarray, optional
            Parameters for self.production
        Returns
        -------
        ndarray
            The value of each box in the carbon box at the specified time_values along with the steady state solution
            for the system
        """
        time_values = jnp.linspace(jnp.min(self.annual), jnp.max(self.annual) + 2,
                                   (self.annual.size + 1) * self.oversample)
        box_values, _ = self.cbm.run(time_values, self.production, y0=y0, args=params,
                                     steady_state_production=self.steady_state_production)
        return box_values

    @partial(jit, static_argnums=(0,))
    def dc14_fine(self, params=()):
        """
        Predict d14c on the same time sampling as self.time_data_fine.
        Parameters
        ----------
        params : ndarray, optional
            Parameters for self.production
        Returns
        -------
        ndarray
            Predicted d14c value
        """
        burnin = self.run_burnin(y0=self.steady_state_y0, params=params)
        event = self.run_event(y0=burnin[-1, :], params=params)
        d14c = (event[:, self.box_idx] - self.steady_state_y0[self.box_idx]) / self.steady_state_y0[self.box_idx] * 1000
        return d14c

    # @partial(jit, static_argnums=(0,))
    def multi_likelihood(self, params):
        """
        Computes the ensemble log-likelihood of the parameters of self.production across multiple d14c datasets
        Parameters
        ----------
        params : ndarray
            Parameters of self.production
        Returns
        -------
        float
            Log-likelihood
        """
        burnin = self.run_burnin(y0=self.steady_state_y0, params=params)
        event = self.run_event(y0=burnin[-1, :], params=params)[:, self.box_idx]
        like = 0
        for sf in self.MultiFitter:
            binned_data = self.cbm.bin_data(event, self.oversample, self.annual, growth=sf.growth)
            d14c = (binned_data - self.steady_state_box) / self.steady_state_box * 1000
            d14c_sf = d14c[sf.multi_mask] + sf.offset
            like += jnp.sum(((sf.d14c_data - d14c_sf) / sf.d14c_data_error) ** 2) * -0.5
        return like

    @partial(jit, static_argnums=(0,))
    def log_likelihood_gp(self, params):
        """
        Computes the Gaussian Process log-likelihood of a set of control-points. The Gaussian Process
        has a constant mean and a Matern-3/2 kernel with fixed parameters.
        Parameters
        ----------
        params : ndarray
            An array of control-points. First control point is also the mean of the Gaussian Process
        Returns
        -------
        float
            Gaussian Process log-likelihood
        """
        kernel = jax_terms.Matern32Term(sigma=2., rho=2.)
        gp = celerite2.jax.GaussianProcess(kernel, mean=params[0])
        gp.compute(self.control_points_time)
        return gp.log_likelihood(params)

    def log_joint_likelihood(self, params, low_bounds, up_bounds):
        """
        Computes the log joint likelihood of parameters of self.production
        Parameters
        ----------
        params : ndarray
            Parameters of self.production
        low_bounds : ndarray
            Lower bound for the parameters of self.production
        up_bounds : ndarray
            Upper bound for the parameters of self.production
        Returns
        -------
        float
            Log joint likelihood
        """
        lp = 0
        lp += jnp.any((params < low_bounds) | (params > up_bounds)) * -jnp.inf
        pos = self.multi_likelihood(params)
        return lp + pos

    def log_joint_likelihood_gp(self, params, low_bounds, up_bounds):
        """
        Computes the negative log joint likelihood of a set of control-points. Used as the objective function for
        training the set of control-points via numerical optimization.
        Parameters
        ----------
        params : ndarray
            An array of control-points. First control point is also the mean of the Gaussian Process
        Returns
        -------
        float
            Negative log joint likelihood
        """
        lp = jnp.any((params < low_bounds) | (params > up_bounds)) * -jnp.inf
        return self.multi_likelihood(params=params) + self.log_likelihood_gp(params) + lp

    def neg_log_joint_likelihood_gp(self, params):
        """
        Computes the negative log joint likelihood of a set of control-points. Used as the objective function for
        training the set of control-points via numerical optimization.
        Parameters
        ----------
        params : ndarray
            An array of control-points. First control point is also the mean of the Gaussian Process
        Returns
        -------
        float
            Negative log joint likelihood
        """
        return -1 * self.multi_likelihood(params=params) + -1 * self.log_likelihood_gp(params)

    def fit_ControlPoints(self, low_bound=0):
        """
        Fits the control-points by minimizing the negative log joint likelihood.
        Parameters
        ----------
        low_bound : int, optional
            The minimum value each control-point can take. 0 by default.
        Returns
        -------
        OptimizeResult
            Scipy OptimizeResult object
        """
        initial = self.steady_state_production * jnp.ones((len(self.control_points_time),))
        bounds = tuple([(low_bound, None)] * len(initial))
        soln = scipy.optimize.minimize(self.neg_log_joint_likelihood_gp, initial, bounds=bounds,
                                       options={'maxiter': 100000, 'maxfun': 100000, })
        return soln.x


def get_data(path=None, event=None):
    """
    Retrieves all the datasets (csv format) in a directory
    Parameters
    ----------
    path : str, optional
        When specified it is the relative path to the directory where the data is stored. Only one of path and event
        should be specified at any time.
    event : str, optional
        Identifier of known Miyake events. When specified it must take value from: '660BCE', '775AD-early', '993AD', '5259BCE',
        '5410BCE', '7176BCE'.
    hemisphere : str, optional
        hemispheric parameter for Carbon Box Model. Used to retrieve the correct data files when event is specified.
    Returns
    -------
    list
        A list of file names to be loaded into SingleFitter
    """
    if path:
        file_names = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
    elif event in ['660BCE-Ew', '660BCE-Lw', '775AD-early-N', '775AD-early-S', '775AD-late-N',
                   '993AD-N', '993AD-S', '5259BCE', '5410BCE', '7176BCE']:
        file = 'data/datasets/' + event
        path = os.path.join(os.path.dirname(__file__), file)
        file_names = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
    else:
        raise ValueError(
            "Invalid path, or event is not from the following: '660BCE-Ew', '660BCE-Lw', '775AD-early', '775AD-late', '993AD', '5259BCE', '5410BCE', '7176BCE'")
    return file_names


def sample_event(year, mf, sampler='MCMC', production_model='simple_sinusoid', burnin=500, production=1000,
                 params=None, low_bounds=None, up_bounds=None):
    """
    Runs Monte Carlo sampler on a Miyake event.
    Parameters
    ----------
    year : float
        The calender year which the Miyake event is supposed to have occurred.
    mf : MultiFitter
        MultiFitter object that enables likelihood function evaluations.
    sampler : str, optional
        Monte Carlo sampler. 'MCMC' for Markov Chain Monte Carlo, 'NS' for Nested Sampling. 'MCMC' by default.
    production_model : str | callable, optional
        The d14c production rate model of the SingleFitters. 'simple_sinusoid' by default.
    burnin : int, optional
        Number of burn-in steps for Markov Chain Monte Carlo, 500 by default.
    production : int, optional
        Number of production steps for Markov Chain Monte Carlo, 1000 by default.
    params : ndarray, optional
        Initial parameters for Monte Carlo samplers. Required when custom production rate model is used.
    low_bounds : ndarray, optional
        Lower bound for parameters. Required when custom production rate model is used.
    up_bounds : ndarray, optional
        Upper bound for parameters. Required when custom production rate model is used.
    Returns
    -------
    result
        MCMC sampler or NS sampler
    """
    if production_model == 'simple_sinusoid':
        default_params = np.array([year, 1. / 12, 3., 81. / 12])
        default_low_bounds = jnp.array([year - 5, 1 / 52., 0, 0.])
        default_up_bounds = jnp.array([year + 5, 5., 11, 15.])
    elif production_model == 'flexible_sinusoid':
        default_params = np.array([year, 1. / 12, 3., 81. / 12, 0.18])
        default_low_bounds = jnp.array([year - 5, 1 / 52., 0, 0., 0.])
        default_up_bounds = jnp.array([year + 5, 5., 11, 15., 2.])
    elif production_model == 'flexible_sinusoid_affine_variant':
        default_params = np.array([0, year, 1. / 12, 3., 81. / 12, 0.18])
        default_low_bounds = jnp.array([-mf.steady_state_production * 0.05 / 5, year - 5, 1 / 52., 0, 0., 0.])
        default_up_bounds = jnp.array([mf.steady_state_production * 0.05 / 5, year + 5, 5., 11, 15., 0.3])
    elif production_model == 'affine':
        default_params = np.array([0, year, 1. / 12, 81. / 12])
        default_low_bounds = jnp.array([-mf.steady_state_production * 0.05 / 5, year - 5, 1 / 52., 0.])
        default_up_bounds = jnp.array([mf.steady_state_production * 0.05 / 5, year + 5, 5., 15.])
    elif production_model == 'control_points':
        if sampler == "MCMC":
            default_params = mf.steady_state_production * jnp.ones((len(mf.control_points_time),))
            default_low_bounds = jnp.array([0] * default_params.size)
            default_up_bounds = jnp.array([100] * default_params.size)
        elif sampler == "optimisation":
            print("Running numerical optimization...")
            return mf, mf.fit_ControlPoints()
    if all(arg is not None for arg in (low_bounds, up_bounds)):
        low_bounds = low_bounds
        up_bounds = up_bounds
    else:
        low_bounds = default_low_bounds
        up_bounds = default_up_bounds

    if params is not None:
        params = params
    else:
        params = default_params

    if sampler == 'MCMC':
        if production_model != 'control_points':
            chain = mf.MarkovChainSampler(params,
                                          likelihood=mf.log_joint_likelihood,
                                          burnin=burnin,
                                          production=production,
                                          args=(low_bounds, up_bounds)
                                          )
        else:
            chain = mf.MarkovChainSampler(params,
                                          likelihood=mf.log_joint_likelihood_gp,
                                          burnin=burnin,
                                          production=production,
                                          args=(low_bounds, up_bounds))
    elif sampler == 'NS':
        print("Running Nested Sampling...")
        chain = mf.NestedSampler(params,
                                 likelihood=mf.multi_likelihood,
                                 low_bound=low_bounds,
                                 high_bound=up_bounds
                                 )
        print("Done")
    else:
        raise ValueError("Invalid sampler value. 'sampler' must be one of the following: MCMC, NS")
    return chain


def fit_event(year, event=None, path=None, production_model='simple_sinusoid', cbm_model='Guttler15', box='Troposphere',
              hemisphere='north', sampler=None, burnin=500, production=1000, params=None, low_bounds=None,
              up_bounds=None, mf=None, oversample=108, burnin_time=2000):
    """
    Fits a Miyake event.
    Parameters
    ----------
    year : float
        The calender year which the Miyake event is supposed to have occurred.
    event : str, optional
        Identifier of known Miyake events. When specified it takes values from: '660BCE', '775AD', '993AD', '5259BCE',
        '5410BCE', '7176BCE'.
    path : str, optional
        When specified it is the relative path to the directory where the data is stored. Only one of path and event
        should be specified at any time.
    production_model : str | callable, optional
        The d14c production rate model of the SingleFitters. 'simple_sinusoid' by default.
    cbm_model : str, optional
        Name of a Carbon Box Model. Must be one from: Miyake17, Brehm21, Guttler15, Buntgen18.
    box : str, optional
        The specific box at which to calculate the d14c. 'Troposphere' by default
    hemisphere : str, optional
        hemispheric parameter for Carbon Box Model. Used to retrieve the correct data files when event is specified.
    sampler : str, optional
        Monte Carlo sampler. 'MCMC' for Markov Chain Monte Carlo, 'NS' for Nested Sampling. 'MCMC' by default.
    burnin : int, optional
        Number of burn-in steps for Markov Chain Monte Carlo, 500 by default.
    production : int, optional
        Number of production steps for Markov Chain Monte Carlo, 1000 by default.
    params : ndarray, optional
        Initial parameters for Monte Carlo samplers. Required when custom production rate model is used.
    low_bounds : ndarray, optional
        Lower bound for parameters. Required when custom production rate model is used.
    up_bounds : ndarray, optional
        Upper bound for parameters. Required when custom production rate model is used.
    mf : MultiFitter
        MultiFitter object that enables likelihood function evaluations. If None, a new MultiFitter Object will be
        initialized
    Returns
    -------
    mf : MultiFitter
        MultiFitter object that enables likelihood function evaluations.
    result : ndrray | NestedSampler Object
        MCMC sampler or NS sampler
    """
    if not mf:
        mf = MultiFitter()
    cbm = ticktack.load_presaved_model(cbm_model, production_rate_units='atoms/cm^2/s')
    if event:
        file_names = get_data(event=event)
        print("Retrieving data...")
        for file in tqdm(file_names):
            file_name = 'data/datasets/' + event + '/' + file
            sf = SingleFitter(cbm, cbm_model, box=box, hemisphere=hemisphere)
            sf.load_data(os.path.join(os.path.dirname(__file__), file_name), oversample=oversample,
                         burnin_time=burnin_time)
            sf.compile_production_model(model=production_model)
            mf.add_SingleFitter(sf)
    elif path:
        file_names = get_data(path=path)
        print("Retrieving data...")
        for file_name in tqdm(file_names):
            sf = SingleFitter(cbm, cbm_model=cbm_model, box=box, hemisphere=hemisphere)
            sf.load_data(path + '/' + file_name, oversample=oversample, burnin_time=burnin_time)
            sf.compile_production_model(model=production_model)
            mf.add_SingleFitter(sf)
    mf.compile()
    if not sampler:
        return mf
    else:
        return mf, sample_event(year, mf, sampler=sampler, params=params, burnin=burnin, production=production,
                                production_model=production_model, low_bounds=low_bounds, up_bounds=up_bounds)


def plot_samples(average_path=None, chains_path=None, cbm_models=None, cbm_label=None, hemisphere="north",
                 production_model=None,
                 directory_path=None, size=100, size2=30, alpha=0.05, alpha2=0.2, savefig_path=None, title=None,
                 axs=None, labels=True, interval=None, capsize=3, markersize=6, elinewidth=3):
    if axs:
        ax1, ax2 = axs
    else:
        fig, (ax1, ax2) = plt.subplots(2, figsize=(8, 8), sharex=True, gridspec_kw={'height_ratios': [2, 1]})
        fig.subplots_adjust(hspace=0.05)
    colors = mpl.rcParams['axes.prop_cycle'].by_key()['color']
    for i, model in enumerate(cbm_models):
        cbm = ticktack.load_presaved_model(model, production_rate_units='atoms/cm^2/s')
        sf = SingleFitter(cbm, cbm_model=model, hemisphere=hemisphere)
        sf.load_data(average_path)
        chain = np.load(chains_path[i])
        sf.compile_production_model(model=production_model)

        if sf.start < 0:
            time_data = sf.time_data * -1 - sf.time_offset
            time_data_fine = sf.time_data_fine * -1
            ax1.invert_xaxis()
            ax2.invert_xaxis()
        else:
            time_data = sf.time_data + sf.time_offset
            time_data_fine = sf.time_data_fine

        idx = np.random.randint(len(chain), size=size)
        for param in chain[idx]:
            ax1.plot(time_data_fine, sf.dc14_fine(params=param), alpha=alpha, color=colors[i])

        for param in chain[idx][:size2]:
            ax2.plot(time_data_fine, sf.production(sf.time_data_fine, *param), alpha=alpha2, color=colors[i])

    ax1.errorbar(time_data, sf.d14c_data, yerr=sf.d14c_data_error, fmt="ok", capsize=capsize, markersize=markersize,
                 elinewidth=elinewidth, label="average $\Delta^{14}$C")

    if directory_path:
        file_names = [f for f in os.listdir(directory_path) if os.path.isfile(os.path.join(directory_path, f))]
        for file in file_names:
            sf.load_data(directory_path + '/' + file)
            if sf.start < 0:
                ax1.errorbar(-sf.time_data - sf.time_offset, sf.d14c_data, fmt="o", color="gray",
                             yerr=sf.d14c_data_error, capsize=capsize,
                             markersize=markersize, alpha=0.2, elinewidth=elinewidth)
            else:
                ax1.errorbar(sf.time_data + sf.time_offset, sf.d14c_data, fmt="o", color="gray",
                             yerr=sf.d14c_data_error, capsize=capsize,
                             markersize=markersize, alpha=0.2, elinewidth=elinewidth)
    sf.load_data(average_path)
    if labels:
        if cbm_label:
            custom_lines = [Line2D([0], [0], color=colors[i], lw=1.5, label=cbm_label[i]) for i in
                            range(len(cbm_label))]
        else:
            custom_lines = [Line2D([0], [0], color=colors[i], lw=1.5, label=cbm_models[i]) for i in
                            range(len(cbm_models))]
        custom_lines.append(Line2D([0], [0], color="k", marker="o", lw=1.5, label="average $\Delta^{14}$C"))
        ax1.legend(handles=custom_lines)
        ax1.set_ylabel("$\Delta^{14}$C (‰)")
        if sf.start < 0:
            ax2.set_xlabel("Year (BCE)");
        else:
            ax2.set_xlabel("Year (AD)");
        ax2.set_ylabel("Production rate (atoms/cm$^2$/s)");
        plt.suptitle(title);
        plt.tight_layout();
    ax2.set_ylim(1, 10);
    if interval:
        if sf.start < 0:
            ax2.set_xlim(-sf.start + 0.2, -sf.end - 0.2);
            ax2.set_xticks(np.arange(-sf.end - 1, -sf.start, interval))
        else:
            ax2.set_xlim(sf.start - 0.2, sf.end + 0.2);
            ax2.set_xticks(np.arange(sf.start, sf.end + 1, interval))
    else:
        if sf.start < 0:
            ax2.set_xlim(-sf.start + 0.2, -sf.end - 0.2);
        else:
            ax2.set_xlim(sf.start - 0.2, sf.end + 0.2);
        ax2.xaxis.set_major_locator(MaxNLocator(integer=True))

    if savefig_path:
        plt.savefig(savefig_path)


def plot_ControlPoints(average_path=None, soln_path=None, chain_path=None, cbm_models=None, cbm_label=None,
                       hemisphere="north", merged_inverse_solver=None,
                       directory_path=None, savefig_path=None, title=None, axs=None, labels=True, interval=None,
                       markersize=6, capsize=3, markersize2=3, elinewidth=3, size=1, alpha=1,):
    if axs:
        ax1, ax2 = axs
    else:
        fig, (ax1, ax2) = plt.subplots(2, dpi=100, figsize=(8, 8),  sharex=True, gridspec_kw={'height_ratios': [2, 1]})
        fig.subplots_adjust(hspace=0.05)
    colors = mpl.rcParams['axes.prop_cycle'].by_key()['color']
    for i, model in enumerate(cbm_models):
        cbm = ticktack.load_presaved_model(model, production_rate_units='atoms/cm^2/s')
        sf = SingleFitter(cbm, cbm_model=model, hemisphere=hemisphere)
        sf.load_data(average_path)
        soln = np.load(soln_path[i], allow_pickle=True)
        sf.compile_production_model(model="control_points")

        if sf.start < 0:
            time_data = sf.time_data * -1 - sf.time_offset
            time_data_fine = sf.time_data_fine * -1
            control_points_time = sf.control_points_time * -1
            control_points_time_fine = sf.control_points_time_fine * -1
            ax1.invert_xaxis()
            ax2.invert_xaxis()
        else:
            time_data = sf.time_data + sf.time_offset
            control_points_time = sf.control_points_time
            control_points_time_fine = sf.control_points_time_fine
            time_data_fine = sf.time_data_fine

        if np.all(merged_inverse_solver is not None):
            ax2.errorbar(time_data, np.median(merged_inverse_solver, axis=0), fmt="k", drawstyle="steps",
                         alpha=0.2)
            ax2.fill_between(time_data, np.percentile(merged_inverse_solver, 32, axis=0),
                             np.percentile(merged_inverse_solver, 68, axis=0), step='pre', alpha=0.1,
                             color="k", edgecolor="none", lw=1.5)

        if chain_path:
            chain = np.load(chain_path[i], allow_pickle=True)
            mu = np.mean(chain, axis=0)
            std = np.std(chain, axis=0)

            if size == 1:
                ax1.plot(time_data_fine, sf.dc14_fine(mu), color=colors[i])
            else:
                idx = np.random.randint(len(chain), size=size)
                for param in chain[idx]:
                    ax1.plot(time_data_fine, sf.dc14_fine(params=param), alpha=alpha, color=colors[i])

            ax2.plot(control_points_time_fine, sf.interp_gp(sf.control_points_time_fine, mu), color=colors[i])
            idx = np.random.randint(len(chain), size=30)
            for param in chain[idx]:
                ax2.plot(control_points_time_fine, sf.interp_gp(sf.control_points_time_fine, param),
                         alpha=0.2, color=colors[i])
            # mean = mu[0]
            # kernel = jax_terms.Matern32Term(sigma=2., rho=2.)
            # gp = celerite2.jax.GaussianProcess(kernel, sf.control_points_time, mean=mean)
            # pred, var = gp.predict(mu, t=sf.control_points_time_fine, return_var=True)
            # ax2.fill_between(control_points_time_fine, pred - np.sqrt(var),
            #                  pred + np.sqrt(var), color=colors[i], alpha=0.2)
        else:
            ax1.plot(time_data_fine, sf.dc14_fine(soln), color=colors[i])
            ax2.plot(control_points_time_fine, sf.interp_gp(sf.control_points_time_fine, soln), color=colors[i])

    ax1.errorbar(time_data, sf.d14c_data, yerr=sf.d14c_data_error, fmt="ok", capsize=capsize,
                 markersize=markersize, elinewidth=elinewidth, label="average $\Delta^{14}$C")

    if directory_path:
        file_names = [f for f in os.listdir(directory_path) if os.path.isfile(os.path.join(directory_path, f))]
        for file in file_names:
            sf.load_data(directory_path + '/' + file)
            if sf.start < 0:
                ax1.errorbar(-sf.time_data - sf.time_offset, sf.d14c_data, fmt="o", color="gray",
                             yerr=sf.d14c_data_error, capsize=capsize,
                             markersize=markersize, alpha=0.2)
            else:
                ax1.errorbar(sf.time_data + sf.time_offset, sf.d14c_data, fmt="o", color="gray",
                             yerr=sf.d14c_data_error, capsize=capsize,
                             markersize=markersize, alpha=0.2)

    sf.load_data(average_path)
    if labels:
        if cbm_label:
            custom_lines = [Line2D([0], [0], color=colors[i], lw=1.5, label=cbm_label[i]) for i in
                            range(len(cbm_label))]
        else:
            custom_lines = [Line2D([0], [0], color=colors[i], lw=1.5, label=cbm_models[i]) for i in
                            range(len(cbm_models))]
        custom_lines.append(Line2D([0], [0], color="k", marker="o", lw=1.5, label="average $\Delta^{14}$C"))
        ax1.legend(handles=custom_lines, frameon=False)
        ax1.set_ylabel("$\Delta^{14}$C (‰)")
        if sf.start < 0:
            ax2.set_xlabel("Year (BCE)");
        else:
            ax2.set_xlabel("Year (AD)");
        ax2.set_ylabel("Production rate (atoms/cm$^2$/s)");
        plt.suptitle(title);
        plt.tight_layout();
    if interval:
        if sf.start < 0:
            ax2.set_xlim(-sf.start + 0.2, -sf.end - 0.2);
            ax2.set_xticks(np.arange(-sf.start, -sf.end - 1, -interval))
        else:
            ax2.set_xlim(sf.start - 0.2, sf.end + 0.2);
            ax2.set_xticks(np.arange(sf.start, sf.end + 1, interval))
    else:
        if sf.start < 0:
            ax2.set_xlim(-sf.start + 0.2, -sf.end - 0.2);
        else:
            ax2.set_xlim(sf.start - 0.2, sf.end + 0.2);
        ax2.xaxis.set_major_locator(MaxNLocator(integer=True))
    if savefig_path:
        plt.savefig(savefig_path)
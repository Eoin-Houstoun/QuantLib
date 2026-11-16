/* Harness driver for the Heston calibration benchmark (Artemis). Not part of QuantLib.

   Built by artemis_bench/setup.sh against this checkout's libQuantLib. The Python parents
   (gate.py, bench.py) never link QuantLib; they read this program's JSON output.

     heston_driver calib     prints ARTEMIS_READY, reads a nonce, runs the 78-option
                             calibration, prints "ARTEMIS_DONE <nonce>", then one JSON line:
                             {"params": [...], "errors": [...]}
     heston_driver engine    same handshake; prices the stress grid ENGINE_REPEATS times
                             through the default engine (no instrument caching), then JSON
                             {"checksum": ...}
     heston_driver grid      untimed; JSON {"prices": [...]} for the stress grid, default engine
     heston_driver reference untimed; the same grid with adaptive Gauss-Lobatto at 1e-12 relative, 1e-14 absolute

   The market and the start point match the 16 Nov profiling script (heston.py): spot 100,
   r 3%, q 1%, true model v0 0.04, kappa 1.5, theta 0.05, sigma 0.6, rho -0.7; quotes are the
   true model's implied vols on 6 maturities x 13 strikes; calibration starts from
   v0 0.1, kappa 1.0, theta 0.1, sigma 0.5, rho -0.5 with LevenbergMarquardt(1e-8, 1e-8, 1e-8)
   and EndCriteria(500, 50, 1e-8, 1e-8, 1e-8).
*/

#include <ql/exercise.hpp>
#include <ql/instruments/payoffs.hpp>
#include <ql/instruments/vanillaoption.hpp>
#include <ql/math/optimization/endcriteria.hpp>
#include <ql/math/optimization/levenbergmarquardt.hpp>
#include <ql/models/equity/hestonmodel.hpp>
#include <ql/models/equity/hestonmodelhelper.hpp>
#include <ql/pricingengines/vanilla/analytichestonengine.hpp>
#include <ql/processes/hestonprocess.hpp>
#include <ql/quotes/simplequote.hpp>
#include <ql/settings.hpp>
#include <ql/termstructures/yield/flatforward.hpp>
#include <ql/time/calendars/target.hpp>
#include <ql/time/daycounters/actual365fixed.hpp>
#include <cstdio>
#include <iostream>
#include <string>
#include <tuple>
#include <vector>

using namespace QuantLib;

namespace {

    const int ENGINE_REPEATS = 20;

    struct Market {
        Date today;
        Calendar calendar;
        DayCounter dayCounter;
        Handle<Quote> spot;
        Handle<YieldTermStructure> riskFree, dividend;
    };

    Market makeMarket() {
        Market m;
        m.today = Date(15, November, 2026);
        Settings::instance().evaluationDate() = m.today;
        m.calendar = TARGET();
        m.dayCounter = Actual365Fixed();
        m.spot = Handle<Quote>(ext::make_shared<SimpleQuote>(100.0));
        m.riskFree = Handle<YieldTermStructure>(
            ext::make_shared<FlatForward>(m.today, 0.03, m.dayCounter));
        m.dividend = Handle<YieldTermStructure>(
            ext::make_shared<FlatForward>(m.today, 0.01, m.dayCounter));
        return m;
    }

    ext::shared_ptr<HestonModel> makeModel(const Market& m, Real v0, Real kappa, Real theta,
                                           Real sigma, Real rho) {
        return ext::make_shared<HestonModel>(ext::make_shared<HestonProcess>(
            m.riskFree, m.dividend, m.spot, v0, kappa, theta, sigma, rho));
    }

    // The 78 market quotes: the true model's implied vols, computed once with the seed's
    // default engine at build time of the inputs (outside any timed region).
    std::vector<std::tuple<Period, Real, Volatility>> marketQuotes(const Market& m) {
        auto truth = makeModel(m, 0.04, 1.5, 0.05, 0.6, -0.7);
        auto engine = ext::make_shared<AnalyticHestonEngine>(truth);
        std::vector<std::tuple<Period, Real, Volatility>> quotes;
        for (Integer months : {1, 3, 6, 12, 24, 36}) {
            for (int i = 0; i < 13; ++i) {
                Real strike = 70.0 + 5.0 * i;
                auto vol = ext::make_shared<SimpleQuote>(0.2);
                HestonModelHelper h(Period(months, Months), m.calendar, 100.0, strike,
                                    Handle<Quote>(vol), m.riskFree, m.dividend);
                h.setPricingEngine(engine);
                Real price = h.modelValue();
                quotes.emplace_back(Period(months, Months), strike,
                                    h.impliedVolatility(price, 1e-12, 5000, 0.001, 4.0));
            }
        }
        return quotes;
    }

    struct StressCase {
        Real v0, kappa, theta, sigma, rho;
    };

    // Regimes chosen to break careless quadrature: high vol of vol with rho near -1, very low
    // variance, a Feller-violating short-dated case, and positive rho.
    const std::vector<StressCase> STRESS_MODELS = {
        {0.04, 1.5, 0.05, 0.6, -0.7},   {0.04, 1.0, 0.04, 1.5, -0.95},
        {0.0025, 3.0, 0.004, 0.2, -0.3}, {0.09, 0.5, 0.09, 1.2, -0.5},
        {0.04, 2.0, 0.05, 0.4, 0.5}};
    const std::vector<Period> STRESS_MATURITIES = {Period(1, Weeks), Period(1, Months),
                                                   Period(3, Months), Period(1, Years),
                                                   Period(5, Years), Period(10, Years)};
    const std::vector<Real> STRESS_MONEYNESS = {0.5, 0.7, 0.85, 1.0, 1.15, 1.4, 2.0};

    template <class EngineFactory>
    std::vector<Real> stressPrices(const Market& m, EngineFactory makeEngine) {
        std::vector<Real> prices;
        for (const auto& s : STRESS_MODELS) {
            auto model = makeModel(m, s.v0, s.kappa, s.theta, s.sigma, s.rho);
            auto engine = makeEngine(model);
            for (const auto& p : STRESS_MATURITIES) {
                auto exercise = ext::make_shared<EuropeanExercise>(m.calendar.advance(m.today, p));
                for (Real k : STRESS_MONEYNESS) {
                    for (Option::Type type : {Option::Call, Option::Put}) {
                        VanillaOption option(
                            ext::make_shared<PlainVanillaPayoff>(type, 100.0 * k), exercise);
                        option.setPricingEngine(engine);
                        prices.push_back(option.NPV());
                    }
                }
            }
        }
        return prices;
    }

    void printArray(const char* key, const std::vector<Real>& xs, bool last) {
        std::printf("\"%s\": [", key);
        for (Size i = 0; i < xs.size(); ++i)
            std::printf("%s%.17g", i ? ", " : "", xs[i]);
        std::printf("]%s", last ? "" : ", ");
    }

    std::string handshake() {
        std::printf("ARTEMIS_READY\n");
        std::fflush(stdout);
        std::string nonce;
        std::getline(std::cin, nonce);
        return nonce;
    }

    void done(const std::string& nonce) {
        std::printf("ARTEMIS_DONE %s\n", nonce.c_str());
        std::fflush(stdout);
    }
}

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: heston_driver calib|engine|grid|reference\n");
        return 2;
    }
    const std::string mode = argv[1];
    try {
        Market m = makeMarket();
        if (mode == "calib") {
            auto quotes = marketQuotes(m);
            auto model = makeModel(m, 0.1, 1.0, 0.1, 0.5, -0.5);
            auto engine = ext::make_shared<AnalyticHestonEngine>(model);
            std::vector<ext::shared_ptr<CalibrationHelper>> helpers;
            std::vector<ext::shared_ptr<BlackCalibrationHelper>> black;
            for (const auto& [period, strike, vol] : quotes) {
                auto h = ext::make_shared<HestonModelHelper>(
                    period, m.calendar, 100.0, strike,
                    Handle<Quote>(ext::make_shared<SimpleQuote>(vol)), m.riskFree, m.dividend);
                h->setPricingEngine(engine);
                helpers.push_back(h);
                black.push_back(h);
            }
            LevenbergMarquardt lm(1e-8, 1e-8, 1e-8);
            std::string nonce = handshake();
            model->calibrate(helpers, lm, EndCriteria(500, 50, 1e-8, 1e-8, 1e-8));
            done(nonce);
            std::vector<Real> params, errors;
            for (Real x : model->params())
                params.push_back(x);
            for (const auto& h : black)
                errors.push_back(h->calibrationError());
            std::printf("{");
            printArray("params", params, false);
            printArray("errors", errors, true);
            std::printf("}\n");
        } else if (mode == "engine") {
            std::vector<std::tuple<ext::shared_ptr<AnalyticHestonEngine>, Time>> cases;
            std::vector<ext::shared_ptr<PlainVanillaPayoff>> payoffs;
            for (const auto& s : STRESS_MODELS) {
                auto engine = ext::make_shared<AnalyticHestonEngine>(
                    makeModel(m, s.v0, s.kappa, s.theta, s.sigma, s.rho));
                for (const auto& p : STRESS_MATURITIES)
                    cases.emplace_back(engine, m.dayCounter.yearFraction(
                                                   m.today, m.calendar.advance(m.today, p)));
            }
            for (Real k : STRESS_MONEYNESS)
                for (Option::Type type : {Option::Call, Option::Put})
                    payoffs.push_back(ext::make_shared<PlainVanillaPayoff>(type, 100.0 * k));
            std::string nonce = handshake();
            Real checksum = 0.0;
            for (int r = 0; r < ENGINE_REPEATS; ++r)
                for (const auto& [engine, t] : cases)
                    for (const auto& payoff : payoffs)
                        checksum += engine->priceVanillaPayoff(payoff, t);
            done(nonce);
            std::printf("{\"checksum\": %.17g}\n", checksum);
        } else if (mode == "grid") {
            auto prices = stressPrices(m, [](const ext::shared_ptr<HestonModel>& model) {
                return ext::make_shared<AnalyticHestonEngine>(model);
            });
            std::printf("{");
            printArray("prices", prices, true);
            std::printf("}\n");
        } else if (mode == "reference") {
            auto prices = stressPrices(m, [](const ext::shared_ptr<HestonModel>& model) {
                // Relative 1e-12 alone never converges on deep out-of-the-money prices
                // near zero, so an absolute floor of 1e-14 (100x under the gate's 1e-12).
                return ext::make_shared<AnalyticHestonEngine>(
                    model, AnalyticHestonEngine::OptimalCV,
                    AnalyticHestonEngine::Integration::gaussLobatto(1e-12, 1e-14, 1000000),
                    1e-40);
            });
            std::printf("{");
            printArray("prices", prices, true);
            std::printf("}\n");
        } else {
            std::fprintf(stderr, "unknown mode %s\n", mode.c_str());
            return 2;
        }
    } catch (std::exception& e) {
        std::fprintf(stderr, "heston_driver: %s\n", e.what());
        return 1;
    }
    return 0;
}

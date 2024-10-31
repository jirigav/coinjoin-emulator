from manager.btc_node import BtcNode
from manager import utils
import manager.commands.genscen
from time import sleep, time
import sys
import random
import os
import json
import argparse
import multiprocessing
import multiprocessing.pool
import math
import shutil
import datetime

from manager.wasabi_clients.joinmarket_client import JoinMarketClientServer


DISTRIBUTOR_UTXOS = 20
BATCH_SIZE = 5
BTC = 100_000_000
SCENARIO = {
    "name": "default",
    "default_version": "joinmarket",
    "rounds": 10,  # the number of coinjoins after which the simulation stops (0 for no limit)
    "blocks": 0,  # the number of mined blocks after which the simulation stops (0 for no limit)
    "wallets": [
        {"funds": [200000, 50000], "type": "taker"},
        {"funds": [3000000], "type": "taker", "delay_blocks": 2},
        {"funds": [1000000, 500000], "type": "maker"},
        {"funds": [3000000, 15000], "type": "maker"},
        {"funds": [1000000, 500000], "type": "maker"},
        {"funds": [3000000, 600000], "type": "maker"},
        {"funds": [200000, 50000], "type": "maker"},
        {"funds": [3000000], "type": "maker"},
        {"funds": [1000000, 500000], "type": "maker"},
        {"funds": [3000000, 15000], "type": "maker"},
        {"funds": [1000000, 500000], "type": "maker"},
        {"funds": [3000000, 600000], "type": "maker"},
    ],
}

args = None
driver = None
node: BtcNode = None
coordinator = None
distributor: JoinMarketClientServer = None
clients = []
versions = set()
invoices = {}

current_round = 0
current_block = 0


def prepare_image(name, path=None):
    prefixed_name = args.image_prefix + name
    if driver.has_image(prefixed_name):
        if args.force_rebuild:
            if args.image_prefix:
                driver.pull(prefixed_name)
                print(f"- image pulled {prefixed_name}")
            else:
                driver.build(name, f"./containers/{name}" if path is None else path)
                print(f"- image rebuilt {prefixed_name}")
        else:
            print(f"- image reused {prefixed_name}")
    elif args.image_prefix:
        driver.pull(prefixed_name)
        print(f"- image pulled {prefixed_name}")
    else:
        driver.build(name, f"./containers/{name}" if path is None else path)
        print(f"- image built {prefixed_name}")


def prepare_images():
    print("Preparing images")
    prepare_image("btc-node")
    prepare_image("joinmarket-client-server")
    prepare_image("irc-server")
    # prepare_client_images()


def start_irc_server():
    name = "irc-server"

    try:
        ip, manager_ports = driver.run(
            name,
            f"{args.image_prefix}irc-server",
            cap_add=["NET_ADMIN"],
            env={},  # Add any necessary environment variables
            ports={6667: 6667},
            cpu=1.0,
            memory=2048,
        )
    except Exception as e:
        print(f"- could not start {name} ({e})")
        raise Exception("Could not start IRC server")



def start_infrastructure():
    print("Starting infrastructure")
    start_irc_server()
    print("- started irc-server")

    btc_node_ip, btc_node_ports = driver.run(
        "btc-node",
        f"{args.image_prefix}btc-node",
        ports={18443: 18443, 18444: 18444},
        cpu=4.0,
        memory=8192,
    )
    global node
    node = BtcNode(
        host=btc_node_ip if args.proxy else args.control_ip,
        port=18443 if args.proxy else btc_node_ports[18443],
        internal_ip=btc_node_ip,
        proxy=args.proxy,
    )
    node.wait_ready()
    print("- started btc-node")
    node.create_wallet("jm_wallet")

    start_distributor()

def start_distributor():
    name = "joinmarket-distributor"
    port = 28183  # Use a specific port for the distributor
    try:
        ip, manager_ports = driver.run(
            name,
            "joinmarket-client-server:latest",
            env={},  # Add any necessary environment variables
            ports={28183: port},
            cpu=1.0,
            memory=2048,
        )
    except Exception as e:
        print(f"- could not start {name} ({e})")
        raise Exception("Could not start distributor")

    global distributor
    distributor = init_joinmarket_clientserver(name=name, port=port)

    start = time()
    if not distributor.wait_wallet(timeout=60):
        print(f"- could not start {name} (application timeout)")
        raise Exception("Could not start distributor")
    print(f"- started distributor")


def fund_distributor(btc_amount):
    print("Funding distributor")
    for _ in range(DISTRIBUTOR_UTXOS):
        node.fund_address(
            distributor.get_new_address(),
            math.ceil(btc_amount * BTC / DISTRIBUTOR_UTXOS) // BTC,
        )

    while (balance := distributor.get_balance()) < btc_amount * BTC:
        sleep(1)
    print(f"- funded (current balance {balance / BTC:.8f} BTC)")


def init_joinmarket_clientserver(name, port, host="localhost", type="maker"):
    return JoinMarketClientServer(name=name, port=port, type=type)


def start_client(idx: int, wallet=None):
    name = f"jcs-{idx:03}"
    port = 28184 + idx
    try:
        ip, manager_ports = driver.run(
            name,
            "joinmarket-client-server:latest",
            env={},
            ports={28183: port},
            cpu=(0.1),
            memory=(768),
        )
    except Exception as e:
        print(f"- could not start {name} ({e})")
        return None

    print(f"driver starting {name}")

    delay = (wallet.get("delay_blocks", 0), wallet.get("delay_rounds", 0))
    stop = (wallet.get("stop_blocks", 0), wallet.get("stop_rounds", 0))
    type = wallet.get("type", "maker")

    client = JoinMarketClientServer(name=name, port=port, type=type, delay=delay, stop=stop)


    start = time()
    if not client.wait_wallet(timeout=60):
        print(
            f"- could not start {name} (application timeout {time() - start} seconds)"
        )
        return None

    print(f"- started {client.name} (wait took {time() - start} seconds)")
    return client


def start_clients(wallets):
    print("Starting clients")
    with multiprocessing.pool.ThreadPool() as pool:
        new_clients = pool.starmap(start_client, enumerate(wallets, start=len(clients)))

        for _ in range(3):
            restart_idx = list(
                map(
                    lambda x: x[0],
                    filter(
                        lambda x: x[1] is None,
                        enumerate(new_clients, start=len(clients)),
                    ),
                )
            )

            if not restart_idx:
                break
            print(f"- failed to start {len(restart_idx)} clients; retrying ...")
            for idx in restart_idx:
                driver.stop(f"wasabi-client-{idx:03}")
            sleep(60)
            restarted_clients = pool.starmap(
                start_client,
                ((idx, wallets[idx - len(clients)]) for idx in restart_idx),
            )
            for idx, client in enumerate(restarted_clients):
                if client is not None:
                    new_clients[restart_idx[idx]] = client
        else:
            new_clients = list(filter(lambda x: x is not None, new_clients))
            print(
                f"- failed to start {len(wallets) - len(new_clients)} clients; continuing ..."
            )
    clients.extend(new_clients)


def prepare_invoices(wallets):
    print("Preparing invoices")
    client_invoices = [
        (client, wallet.get("funds", [])) for client, wallet in zip(clients, wallets)
    ]

    global invoices
    for client, funds in client_invoices:
        for fund in funds:
            block = 0
            round = 0
            if isinstance(fund, int):
                value = fund
            elif isinstance(fund, dict):
                value = fund.get("value", 0)
                block = fund.get("delay_blocks", 0)
                round = fund.get("delay_rounds", 0)
            addressed_invoice = (client.get_new_address(), value)
            if (block, round) not in invoices:
                invoices[(block, round)] = [addressed_invoice]
            else:
                invoices[(block, round)].append(addressed_invoice)

    for addressed_invoices in invoices.values():
        random.shuffle(addressed_invoices)

    print(f"- prepared {sum(map(len, invoices.values()))} invoices")


def pay_invoices(addressed_invoices):
    print(
        f"- paying {len(addressed_invoices)} invoices (batch size {BATCH_SIZE}, block {current_block}, round {current_round})"
    )
    try:
        for batch in utils.batched(addressed_invoices, BATCH_SIZE):
            for _ in range(3):
                try:
                    result = distributor.send(batch)
                    if str(result) == "timeout":
                        print("- transaction timeout")
                        continue
                    break
                except Exception as e:
                    # https://github.com/zkSNACKs/WalletWasabi/issues/12764
                    if "Bad Request" in str(e):
                        print("- transaction error (bad request)")
                    else:
                        print(f"- transaction error ({e})")
            else:
                print("- invoice payment failed")
                raise Exception("Invoice payment failed")

    except Exception as e:
        print("- invoice payment failed")
        raise e


def store_client_logs(client, data_path, src_path="/home/wasabi/.walletwasabi/client/"):
    sleep(random.random() * 3)
    client_path = os.path.join(data_path, client.name)
    os.mkdir(client_path)
    with open(os.path.join(client_path, "coins.json"), "w") as f:
        json.dump(client.list_coins(), f, indent=2)
        print(f"- stored {client.name} coins")
    with open(os.path.join(client_path, "unspent_coins.json"), "w") as f:
        json.dump(client.list_unspent_coins(), f, indent=2)
        print(f"- stored {client.name} unspent coins")
    with open(os.path.join(client_path, "keys.json"), "w") as f:
        json.dump(client.list_keys(), f, indent=2)
        print(f"- stored {client.name} keys")
    try:
        driver.download(client.name, src_path, client_path)

        print(f"- stored {client.name} logs")
    except:
        print(f"- could not store {client.name} logs")


def store_logs(src_path="/home/wasabi/.walletwasabi/client/"):
    print("Storing logs")
    time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    experiment_path = f"./logs/{time}_{SCENARIO['name']}"
    data_path = os.path.join(experiment_path, "data")
    os.makedirs(data_path)

    with open(os.path.join(experiment_path, "scenario.json"), "w") as f:
        json.dump(SCENARIO, f, indent=2)
        print("- stored scenario")

    stored_blocks = 0
    node_path = os.path.join(data_path, "btc-node")
    os.mkdir(node_path)
    while stored_blocks < node.get_block_count():
        block_hash = node.get_block_hash(stored_blocks)
        block = node.get_block_info(block_hash)
        with open(os.path.join(node_path, f"block_{stored_blocks}.json"), "w") as f:
            json.dump(block, f, indent=2)
        stored_blocks += 1
    print(f"- stored {stored_blocks} blocks")

    try:
        driver.download(
            "wasabi-backend",
            "/home/wasabi/.walletwasabi/backend/",
            os.path.join(data_path, "wasabi-backend"),
        )

        print(f"- stored backend logs")
    except:
        print(f"- could not store backend logs")

    # TODO parallelize (driver cannot be simply passed to new threads)
    for client in clients:
        store_client_logs(client, data_path, src_path)

    shutil.make_archive(experiment_path, "zip", *os.path.split(experiment_path))
    print("- zip archive created")


def stop_coinjoins():
    print("Stopping coinjoins")
    for client in clients:
        client.stop_coinjoin()
        print(f"- stopped mixing {client.name}")


def update_coinjoins_joinmarket():
    global current_round
    global current_block

    for client in clients:
        state = client.get_status()
        # print(state)
        if client.type == "maker" and not client.maker_running and not client.delay[0] > current_block:
            client.start_maker(0, 5000, 0.00004, "sw0reloffer", 30000)
            print(f"Starting maker {client.name}")

        if client.type == "taker" and not client.coinjoin_in_process and not client.delay[0] > current_block:
            current_round += 1
            address = client.get_new_address()
            client.start_coinjoin(0, 40000, 4, address)
            client.coinjoin_start = current_block
            print(f"Starting coinjoin {client.name}")

        if client.type == "taker" and client.coinjoin_in_process and client.coinjoin_start + 4 < current_block:
            current_round -= 1
            client.stop_coinjoin()
            client.coinjoin_in_process = False
            print(f"Stopping coinjoin {client.name}")

def update_invoice_payments():
    due = list(
        filter(
            lambda x: x[0] <= current_block and x[1] <= current_round, invoices.keys()
        )
    )
    for i in due:
        pay_invoices(invoices.pop(i, []))


def run_joinmarket():
    global current_block
    update_invoice_payments()
    initial_block = node.get_block_count()
    for i in range(5):
        # Takers need 3 confirmations of transactions for the sourcing commitments
        node.mine_block()

    while ( SCENARIO["rounds"] == 0 or current_round < SCENARIO["rounds"] ) and (
            SCENARIO["blocks"] == 0 or current_block < SCENARIO["blocks"]):
        for _ in range(3):
            try:
                current_block = node.get_block_count() - initial_block
                break
            except Exception as e:
                print(f"- could not get blocks".ljust(60), end="\r")
                print(f"Block exception: {e}", file=sys.stderr)

        update_invoice_payments()
        update_coinjoins_joinmarket()

        print(
            f"- coinjoin rounds: {current_round} (block {current_block})".ljust(60),
            end="\r",
        )
        sleep(1)

    print()
    print(f"- limit reached")
    sleep(60)
    node.mine_block()


def run():
    try:
        print(f"=== Scenario {SCENARIO['name']} ===")
        prepare_images()
        start_infrastructure()
        fund_distributor(1000)
        start_clients(SCENARIO["wallets"])
        prepare_invoices(SCENARIO["wallets"])

        print("Running simulation")

        run_joinmarket()

    except KeyboardInterrupt:
        print()
        print("KeyboardInterrupt received")
    except Exception as e:
        print(f"Terminating exception: {e}", file=sys.stderr)
    finally:
        # stop_coinjoins()
        if not args.no_logs:
            store_logs("/home/joinmarket/")
        driver.cleanup(args.image_prefix)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run coinjoin simulation setup")
    subparsers = parser.add_subparsers(dest="command", title="command")

    parser.add_argument(
        "--driver",
        type=str,
        choices=["docker", "podman", "kubernetes"],
        default="docker",
    )
    parser.add_argument("--no-logs", action="store_true", default=False)

    console_subparser = subparsers.add_parser("console", help="run console")
    console_subparser.add_argument(
        "--force-rebuild", action="store_true", help="force rebuild of images"
    )
    console_subparser.add_argument("--namespace", type=str, default="coinjoin")
    console_subparser.add_argument(
        "--image-prefix", type=str, default="", help="image prefix"
    )
    console_subparser.add_argument("--proxy", type=str, default="")
    console_subparser.add_argument(
        "--btc-node-ip", type=str, help="override btc-node ip", default=""
    )
    console_subparser.add_argument(
        "--control-ip", type=str, help="control ip", default="localhost"
    )
    console_subparser.add_argument("--reuse-namespace", action="store_true", default=False)



    build_subparser = subparsers.add_parser("build", help="build images")
    build_subparser.add_argument(
        "--force-rebuild", action="store_true", help="force rebuild of images"
    )
    build_subparser.add_argument("--namespace", type=str, default="coinjoin")
    build_subparser.add_argument(
        "--image-prefix", type=str, default="", help="image prefix"
    )

    run_subparser = subparsers.add_parser("run", help="run simulation")
    run_subparser.add_argument(
        "--force-rebuild", action="store_true", help="force rebuild of images"
    )
    run_subparser.add_argument(
        "--image-prefix", type=str, default="", help="image prefix"
    )
    run_subparser.add_argument(
        "--scenario", type=str, help="scenario specification file"
    )
    run_subparser.add_argument(
        "--btc-node-ip", type=str, help="override btc-node ip", default=""
    )
    run_subparser.add_argument(
        "--wasabi-backend-ip",
        type=str,
        help="override wasabi-backend ip",
        default="",
    )
    run_subparser.add_argument(
        "--control-ip", type=str, help="control ip", default="localhost"
    )
    run_subparser.add_argument("--proxy", type=str, default="")
    run_subparser.add_argument("--namespace", type=str, default="coinjoin")
    run_subparser.add_argument("--reuse-namespace", action="store_true", default=False)

    clean_subparser = subparsers.add_parser("clean", help="clean up")
    clean_subparser.add_argument("--namespace", type=str, default="coinjoin")
    clean_subparser.add_argument(
        "--reuse-namespace", action="store_true", default=False
    )
    clean_subparser.add_argument("--proxy", type=str, default="")
    clean_subparser.add_argument(
        "--image-prefix", type=str, default="", help="image prefix"
    )

    genscen_subparser = subparsers.add_parser("genscen", help="generate scenario file")
    manager.commands.genscen.setup_parser(genscen_subparser)

    args = parser.parse_args()

    if args.command == "genscen":
        manager.commands.genscen.handler(args)
        exit(0)

    match args.driver:
        case "docker":
            from manager.driver.docker import DockerDriver

            driver = DockerDriver("coinjoin")
            driver = DockerDriver(args.namespace)
        case "podman":
            from manager.driver.podman import PodmanDriver

            driver = PodmanDriver()
        case "kubernetes":
            from manager.driver.kubernetes import KubernetesDriver

            driver = KubernetesDriver(args.namespace, args.reuse_namespace)
        case _:
            print(f"Unknown driver '{args.driver}'")
            exit(1)

    if args.command == "run":
        if args.scenario:
            with open(args.scenario) as f:
                SCENARIO.update(json.load(f))

    versions.add(SCENARIO["default_version"])
    if "distributor_version" in SCENARIO:
        versions.add(SCENARIO["distributor_version"])
    for wallet in SCENARIO["wallets"]:
        if "version" in wallet:
            versions.add(wallet["version"])

    match args.command:
        case "build":
            prepare_images()
        case "clean":
            driver.cleanup(args.image_prefix)
        case "run":
            run()
        case _:
            print(f"Unknown command '{args.command}'")
            exit(1)
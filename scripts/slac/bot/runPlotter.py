import logging
from time import sleep
from lsst.daf.butler import Butler

logger = logging.getLogger('lsst.rubintv.production.plotter')

while True:
    try:
        filename = '/scratch/test_plotter.txt'
        msg = 'This is the test message'
        with open(filename, 'w') as f:
            f.write(msg)

        logger.warning('Wrote file to scratch.')
        print('Wrote file to scratch.')

        with open(filename, 'r') as f:
            read = f.read()

        logger.warning('Read file back from scratch.')
        print('Read file back from scratch.')

        assert read==msg

        logger.warning('Data was the same.')
        print(f'Data was the same.')
        sleep(600)

    except Exception as e:
        raise e

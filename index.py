import boto3
import json
import seaborn as sns
import matplotlib.pyplot as plt
from flask import Flask, request, jsonify
from io import BytesIO
import os
import concurrent.futures
import requests
import paramiko
import configparser

# Read configuration from .cred file
config = configparser.ConfigParser()
config.read('.cred')

# Initialize the boto3 client with the specific region
lambda_client = boto3.client('lambda', region_name='us-east-1')
s3_client = boto3.client('s3', region_name='us-east-1')
ec2_client = boto3.client('ec2', region_name='us-east-1')
bucket_name = 'cloudcwfinal'

app = Flask(__name__)

# In-memory storage for var95, var99, and profit/loss values
var95_storage = []
var99_storage = []
profit_loss_storage = []

# EC2 configuration from .cred file
ec2_key_name = config['ec2']['key_name']
ec2_private_key_path = config['ec2']['private_key_path']
ec2_ami_id = config['ec2']['ami_id']
ec2_instance_type = config['ec2']['instance_type']
github_repo = config['ec2']['github_repo']
ec2_instance_id = None  # Store the EC2 instance ID

@app.route('/analyse', methods=['POST'])
def analyse():
    content = request.json
    print(f"Received payload: {content}")

    s = content.get('s', 'lambda')
    minhistory = content['h']
    shots = content['d']
    signal_type = content['t']
    position = content['p']

    # Validate signal type
    if signal_type.lower() not in ['buy', 'sell']:
        error_msg = 'Invalid signal type'
        print(f"Error: {error_msg}")
        return jsonify({'error': error_msg}), 400

    if s == 'lambda':
        return run_simulation_on_lambda(minhistory, shots)
    elif s == 'ec2':
        return run_simulation_on_ec2(minhistory, shots)
    else:
        error_msg = 'Invalid service type'
        print(f"Error: {error_msg}")
        return jsonify({'error': error_msg}), 400

def run_simulation_on_lambda(minhistory, shots):
    payload = {
        'mean': 0.001,
        'std': 0.02,
        'shots': shots
    }
    print(f"Invoking Lambda with payload: {payload}")

    try:
        response = lambda_client.invoke(
            FunctionName='cloudcw1',
            InvocationType='RequestResponse',
            Payload=json.dumps(payload)
        )
        response_payload = json.loads(response['Payload'].read())
        print(f"Lambda response payload: {response_payload}")

        if 'body' not in response_payload:
            print("Error: 'body' not in Lambda response payload.")
            return jsonify({'error': 'Invalid response from Lambda'}), 500

        lambda_result = json.loads(response_payload['body'])
        print(f"Lambda result: {lambda_result}")

        # Store var95 and var99 values
        var95_list = lambda_result['var95']
        var99_list = lambda_result['var99']

        # Save the var95 and var99 values to the storage
        var95_storage.extend(var95_list)
        var99_storage.extend(var99_list)

        # Calculate and store profit/loss values
        profit_loss_list = [(var95 - var99) for var95, var99 in zip(var95_list, var99_list)]
        profit_loss_storage.extend(profit_loss_list)

        return jsonify({'result': 'ok'}), 200

    except Exception as e:
        print(f"Error invoking Lambda function: {e}")
        return jsonify({'error': str(e)}), 500

def run_simulation_on_ec2(minhistory, shots):
    global ec2_instance_id
    if not ec2_instance_id:
        ec2_instance_id = create_ec2_instance()
        if not ec2_instance_id:
            return jsonify({'error': 'Failed to create EC2 instance'}), 500

    # Wait for the instance to be running
    ec2_client.get_waiter('instance_running').wait(InstanceIds=[ec2_instance_id])
    print(f"EC2 instance {ec2_instance_id} is running")

    # Get the public DNS of the instance
    instance_info = ec2_client.describe_instances(InstanceIds=[ec2_instance_id])
    public_dns = instance_info['Reservations'][0]['Instances'][0]['PublicDnsName']
    print(f"EC2 instance public DNS: {public_dns}")

    # SSH into the EC2 instance and run the simulation
    key = paramiko.RSAKey.from_private_key_file(ec2_private_key_path)
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_client.connect(public_dns, username='ec2-user', pkey=key)

    payload = json.dumps({
        'mean': 0.001,
        'std': 0.02,
        'shots': shots
    })

    # Run the script using the payload
    stdin, stdout, stderr = ssh_client.exec_command(f"python3 /home/ec2-user/your-project/simulate.py '{payload}'")
    output = stdout.read().decode()
    error = stderr.read().decode()
    print(output)
    print(error)

    if error:
        return jsonify({'error': error}), 500

    # Assuming the script writes results to a file /home/ec2-user/your-project/results.json
    sftp_client = ssh_client.open_sftp()
    sftp_client.get('/home/ec2-user/your-project/results.json', '/tmp/results.json')
    sftp_client.close()

    with open('/tmp/results.json', 'r') as f:
        ec2_result = json.load(f)
        print(f"EC2 result: {ec2_result}")

    ssh_client.close()

    var95_list = ec2_result['var95']
    var99_list = ec2_result['var99']

    var95_storage.extend(var95_list)
    var99_storage.extend(var99_list)

    profit_loss_list = [(var95 - var99) for var95, var99 in zip(var95_list, var99_list)]
    profit_loss_storage.extend(profit_loss_list)

    return jsonify({'result': 'ok'}), 200

def create_ec2_instance():
    try:
        response = ec2_client.run_instances(
            ImageId=ec2_ami_id,
            InstanceType=ec2_instance_type,
            KeyName=ec2_key_name,
            MinCount=1,
            MaxCount=1,
            UserData=f'''#!/bin/bash
                        sudo yum update -y
                        sudo yum install -y python3 python3-pip git
                        git clone https://github.com/pkxsykix/cloudcwfinal20.git
                        cd cloudcwfinal20
                        pip3 install -r requirements.txt
                        python3 simulate.py > /home/ec2-user/simulate.log 2>&1
                        ls /home/ec2-user/cloudcwfinal20 > /home/ec2-user/project_files.log
                        '''
        )
        instance_id = response['Instances'][0]['InstanceId']
        print(f"EC2 instance {instance_id} created")
        return instance_id
    except Exception as e:
        print(f"Error creating EC2 instance: {e}")
        return None

@app.route('/warmup', methods=['POST'])
def warmup():
    content = request.json
    service = content.get('s')
    runs = content.get('r', 1)

    if service == 'lambda':
        payload = {
            'mean': 0.001,
            'std': 0.02,
            'shots': 1
        }
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=runs) as executor:
                futures = [executor.submit(lambda_client.invoke,
                                           FunctionName='cloudcw1',
                                           InvocationType='RequestResponse',
                                           Payload=json.dumps(payload)) for _ in range(runs)]
                results = [future.result() for future in concurrent.futures.as_completed(futures)]
            return jsonify({'result': 'ok'}), 200
        except Exception as e:
            print(f"Error invoking Lambda function: {e}")
            return jsonify({'error': str(e)}), 500
    elif service == 'ec2':
        global ec2_instance_id
        ec2_instance_id = create_ec2_instance()
        if not ec2_instance_id:
            return jsonify({'error': 'Failed to create EC2 instance'}), 500

        # Wait for the instance to be running and setup
        ec2_client.get_waiter('instance_status_ok').wait(InstanceIds=[ec2_instance_id])
        print(f"EC2 instance {ec2_instance_id} is running and set up")

        # Retrieve results from EC2
        public_dns = ec2_client.describe_instances(InstanceIds=[ec2_instance_id])['Reservations'][0]['Instances'][0]['PublicDnsName']
        key = paramiko.RSAKey.from_private_key_file(ec2_private_key_path)
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(public_dns, username='ec2-user', pkey=key)

        sftp_client = ssh_client.open_sftp()
        sftp_client.get('/home/ec2-user/cloudcwfinal20/results.json', '/tmp/results.json')
        sftp_client.close()
        ssh_client.close()

        with open('/tmp/results.json', 'r') as f:
            ec2_result = json.load(f)
            print(f"EC2 result: {ec2_result}")

        var95_list = ec2_result['var95']
        var99_list = ec2_result['var99']

        var95_storage.extend(var95_list)
        var99_storage.extend(var99_list)

        profit_loss_list = [(var95 - var99) for var95, var99 in zip(var95_list, var99_list)]
        profit_loss_storage.extend(profit_loss_list)

        return jsonify({'result': 'ok'}), 200
    else:
        return jsonify({'error': 'Invalid service type'}), 400

@app.route('/get_sig_vars9599', methods=['GET'])
def get_sig_vars9599():
    return jsonify({
        'var95': var95_storage,
        'var99': var99_storage
    }), 200

@app.route('/get_avg_vars9599', methods=['GET'])
def get_avg_vars9599():
    if len(var95_storage) == 0 or len(var99_storage) == 0:
        return jsonify({'error': 'No data available'}), 400

    avg_var95 = sum(var95_storage) / len(var95_storage)
    avg_var99 = sum(var99_storage) / len(var99_storage)

    return jsonify({
        'var95': avg_var95,
        'var99': avg_var99
    }), 200

@app.route('/get_sig_profit_loss', methods=['GET'])
def get_sig_profit_loss():
    return jsonify({
        'profit_loss': profit_loss_storage
    }), 200

@app.route('/get_tot_profit_loss', methods=['GET'])
def get_tot_profit_loss():
    total_profit_loss = sum(profit_loss_storage)
    return jsonify({
        'profit_loss': total_profit_loss
    }), 200

@app.route('/get_chart_url', methods=['GET'])
def get_chart_url():
    if not var95_storage or not var99_storage:
        return jsonify({'error': 'No data available'}), 400

    # Set the style
    sns.set(style="whitegrid")

    plt.figure(figsize=(12, 6))
    plt.plot(var95_storage, label='VaR 95%', color='blue', marker='o', linestyle='-', linewidth=1, markersize=4)
    plt.plot(var99_storage, label='VaR 99%', color='red', marker='x', linestyle='--', linewidth=1, markersize=4)
    plt.xlabel('Index', fontsize=14)
    plt.ylabel('Value at Risk', fontsize=14)
    plt.title('VaR 95% and 99%', fontsize=16)
    plt.legend()
    plt.grid(True)

    # Save the plot to a BytesIO object
    img_data = BytesIO()
    plt.savefig(img_data, format='png', bbox_inches='tight')
    img_data.seek(0)

    # Upload the plot to S3
    file_name = 'var_chart.png'
    try:
        s3_client.put_object(Bucket=bucket_name, Key=file_name, Body=img_data, ContentType='image/png', ACL='public-read')
        chart_url = f"https://{bucket_name}.s3.amazonaws.com/{file_name}"
        return jsonify({'url': chart_url}), 200
    except Exception as e:
        print(f"Error uploading chart to S3: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_endpoints', methods=['GET'])
def get_endpoints():
    base_url = request.host_url.rstrip('/')
    endpoints_info = {
        '/analyse': f'curl -X POST -H "Content-Type: application/json" -d \'{{"h": "value", "d": "value", "t": "value", "p": "value"}}\' {base_url}/analyse',
        '/get_sig_vars9599': f'curl {base_url}/get_sig_vars9599',
        '/get_avg_vars9599': f'curl {base_url}/get_avg_vars9599',
        '/get_sig_profit_loss': f'curl {base_url}/get_sig_profit_loss',
        '/get_tot_profit_loss': f'curl {base_url}/get_tot_profit_loss',
        '/get_chart_url': f'curl {base_url}/get_chart_url',
        '/get_chart/<filename>': f'curl {base_url}/get_chart/<filename>',
        '/warmup': f'curl -X POST -H "Content-Type: application/json" -d \'{{"s": "lambda", "r": 3}}\' {base_url}/warmup'
    }

    endpoints = [{'endpoint': endpoint, 'callstring': callstring} for endpoint, callstring in endpoints_info.items()]
    
    return jsonify(endpoints), 200

if __name__ == '__main__':
    app.run(debug=True, port=5000)

- Username: admin
- Password: a32f49d628fc68ed3acd46c0

docker build -t dashboard:0.1.1 services/dashboard
k3d image import dashboard:0.1.1 -c store-check
kubectl -n mercora rollout restart deploy/team-ui-dashboard
